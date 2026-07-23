#!/usr/bin/env python3
"""Google Drive upload CLI for the personal-finance program. Uploads a local
file (e.g. the retirement `.xlsx` modeling workbook) straight from a path via
the Drive API — no base64-through-context, works unattended once authorized.

Why this exists: the claude.ai Google Drive MCP tool only accepts content
*inline* (textContent / base64Content), which is impractical for a ~70KB binary
workbook (~93KB of base64 as a single tool argument). This CLI holds its own
OAuth token (like the vendored monarch server) and uploads resumably from disk.

One-time setup (only the user can do this):
  1. In Google Cloud Console, create a project, enable the Google Drive API.
  2. Create an OAuth client ID of type "Desktop app"; download the client-secret
     JSON to `state/google/drive_client_secret.json` (or set
     GOOGLE_DRIVE_CLIENT_SECRET to its path).
  3. Run `python scripts/drive_upload.py auth` in a terminal and complete the
     browser consent. This writes the refresh token to
     `state/google/drive_token.json`. After that, all commands run unattended.

Commands (each prints JSON to stdout):
  auth                                  interactive one-time authorization
  status                                report whether a valid token exists
  find-folder --parent <id> --name <n>  find-or-create a subfolder -> {"id": ...}
  upload --file <path> --parent <id> [--name <title>]
                                        create a new file -> {"id","webViewLink","name"}
  update --file <path> --file-id <id>   replace an existing file's content in
                                        place (keeps id + Drive revision history)
                                        -> {"id","webViewLink","name"}
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GOOGLE_DIR = REPO_ROOT / "state" / "google"
DEFAULT_CLIENT_SECRET = GOOGLE_DIR / "drive_client_secret.json"
DEFAULT_TOKEN = GOOGLE_DIR / "drive_token.json"
FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _err(msg: str, code: int = 1) -> int:
    print(json.dumps({"error": msg}), file=sys.stderr)
    return code


def _client_secret_path() -> Path:
    return Path(os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", str(DEFAULT_CLIENT_SECRET)))


def _token_path() -> Path:
    return Path(os.environ.get("GOOGLE_DRIVE_TOKEN", str(DEFAULT_TOKEN)))


def _load_credentials(interactive: bool):
    """Load stored credentials, refreshing or (if interactive) running the OAuth
    flow as needed. Raises RuntimeError with an actionable message otherwise."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = _token_path()
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not interactive:
        raise RuntimeError(
            "No valid Google Drive token. Run `python scripts/drive_upload.py auth` "
            "in a terminal to authorize (one-time)."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = _client_secret_path()
    if not secret.exists():
        raise RuntimeError(
            f"OAuth client secret not found at {secret}. Create a Desktop-app OAuth "
            "client in Google Cloud Console (Drive API enabled) and save its JSON there, "
            "or set GOOGLE_DRIVE_CLIENT_SECRET."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(interactive: bool = False):
    from googleapiclient.discovery import build
    creds = _load_credentials(interactive=interactive)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _guess_mime(path: Path) -> str:
    if path.suffix.lower() == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def cmd_auth(_args) -> int:
    _load_credentials(interactive=True)
    print(json.dumps({"status": "authorized", "token": str(_token_path())}))
    return 0


def cmd_status(_args) -> int:
    try:
        _load_credentials(interactive=False)
        print(json.dumps({"status": "ok", "token": str(_token_path())}))
        return 0
    except Exception as e:  # noqa: BLE001 - report as data
        return _err(str(e))


def cmd_find_folder(args) -> int:
    svc = _service()
    safe = args.name.replace("'", "\\'")
    q = (f"name = '{safe}' and '{args.parent}' in parents and "
         f"mimeType = '{FOLDER_MIME}' and trashed = false")
    res = svc.files().list(q=q, fields="files(id,name)",
                           supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = res.get("files", [])
    if files:
        print(json.dumps({"id": files[0]["id"], "name": files[0]["name"], "created": False}))
        return 0
    meta = {"name": args.name, "mimeType": FOLDER_MIME, "parents": [args.parent]}
    folder = svc.files().create(body=meta, fields="id,name", supportsAllDrives=True).execute()
    print(json.dumps({"id": folder["id"], "name": folder["name"], "created": True}))
    return 0


def cmd_upload(args) -> int:
    from googleapiclient.http import MediaFileUpload
    path = Path(args.file)
    if not path.exists():
        return _err(f"file not found: {path}")
    svc = _service()
    name = args.name or path.name
    media = MediaFileUpload(str(path), mimetype=_guess_mime(path), resumable=True)
    body = {"name": name, "parents": [args.parent]}
    f = svc.files().create(body=body, media_body=media,
                           fields="id,name,webViewLink", supportsAllDrives=True).execute()
    print(json.dumps({"id": f["id"], "name": f["name"], "webViewLink": f.get("webViewLink")}))
    return 0


def cmd_update(args) -> int:
    from googleapiclient.http import MediaFileUpload
    path = Path(args.file)
    if not path.exists():
        return _err(f"file not found: {path}")
    svc = _service()
    media = MediaFileUpload(str(path), mimetype=_guess_mime(path), resumable=True)
    body = {}
    if args.name:
        body["name"] = args.name
    f = svc.files().update(fileId=args.file_id, media_body=media, body=body or None,
                           fields="id,name,webViewLink", supportsAllDrives=True).execute()
    print(json.dumps({"id": f["id"], "name": f["name"], "webViewLink": f.get("webViewLink")}))
    return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Google Drive upload CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth").set_defaults(func=cmd_auth)
    sub.add_parser("status").set_defaults(func=cmd_status)

    p = sub.add_parser("find-folder")
    p.add_argument("--parent", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_find_folder)

    p = sub.add_parser("upload")
    p.add_argument("--file", required=True)
    p.add_argument("--parent", required=True)
    p.add_argument("--name")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("update")
    p.add_argument("--file", required=True)
    p.add_argument("--file-id", required=True)
    p.add_argument("--name")
    p.set_defaults(func=cmd_update)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001 - surface as clean JSON error
        return _err(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(_main())
