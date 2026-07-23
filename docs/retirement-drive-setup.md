# Google Drive upload — one-time setup

The retirement modeling workbook (`.xlsx`) is uploaded to Google Drive by
`scripts/drive_upload.py`, which holds its **own** Google OAuth token (the
claude.ai Drive connector's auth can't be shared with a local script). This is a
one-time setup — after it's done, uploads run unattended (including the scheduled
quarterly review).

Why not the Drive MCP tool? It only accepts content *inline* (base64), which is
impractical for a ~70 KB binary workbook (~93 KB of base64 as a single argument).
The CLI uploads straight from the file path and can update one canonical file in
place, so Drive keeps the version history as the archive.

## Prerequisite (one-time, already done on this machine)

```
python -m pip install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
```

## Steps (you do this once, in a browser + terminal)

1. **Google Cloud project + Drive API**
   - Go to <https://console.cloud.google.com/>, create a project (or reuse one).
   - APIs & Services ▸ Library ▸ enable **Google Drive API**.

2. **OAuth consent screen** (if not already configured)
   - APIs & Services ▸ OAuth consent screen ▸ External ▸ fill the minimum
     (app name, your email) ▸ add your Google account as a **Test user**.
     (Test-user mode is fine; no verification needed for personal use.)

3. **OAuth client (Desktop app)**
   - APIs & Services ▸ Credentials ▸ Create credentials ▸ **OAuth client ID** ▸
     Application type **Desktop app**.
   - Download the JSON and save it to:
     ```
     state/google/drive_client_secret.json
     ```
     (`state/google/` is git-ignored — the secret and token never get committed.)

4. **Authorize** — in a terminal at the repo root:
   ```
   python scripts/drive_upload.py auth
   ```
   A browser window opens; sign in and approve. On success it writes
   `state/google/drive_token.json` and prints `{"status": "authorized"}`.

5. **Verify:**
   ```
   python scripts/drive_upload.py status
   # -> {"status": "ok", ...}
   ```

That's it. The `retirement` agent will now upload/update the workbook on its own.
If the token is ever revoked or expires without a refresh token, re-run
`python scripts/drive_upload.py auth`.

## What the CLI does (reference)

```
python scripts/drive_upload.py find-folder --parent <folderId> --name Retirement
python scripts/drive_upload.py upload --file <path>.xlsx --parent <folderId> --name "Retirement-Model.xlsx"
python scripts/drive_upload.py update --file <path>.xlsx --file-id <fileId>   # in-place, keeps version history
python scripts/drive_upload.py status
```

Scope is full `drive` (needed to write into the pre-existing shared finance
folder and to update the canonical file in place). Everything runs against your
own Google account.
