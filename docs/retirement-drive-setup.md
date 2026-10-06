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
   - **Known issue: weekly re-login.** While the app's publishing status is
     **Testing**, Google expires its refresh tokens after **7 days**, so
     `drive_upload.py auth` has to be re-run about weekly (this is what broke the
     Drive upload on 2026-08-16). To stop that, set the publishing status to
     **In production** (OAuth consent screen / Google Auth platform ▸ Audience ▸
     *Publish app*). For a single-user personal app you do not need Google's
     verification: you click through the "Google hasn't verified this app" warning
     once. Tokens issued while the app was in Testing keep the 7-day limit, so
     re-run `python scripts/drive_upload.py auth` once after switching. Until this
     is done, `scripts/credential_check.py` reminds you before each expiry; if you
     do switch, raise Drive's `lifetime_days` in `state/credential_check.json`
     (or watch whether it stays alive past 7 days) so the reminders stop.

3. **OAuth client (Desktop app)**
   - APIs & Services ▸ Credentials ▸ Create credentials ▸ **OAuth client ID** ▸
     Application type **Desktop app**.
   - Download the JSON and save it to:
     ```
     ~/.config/scherbring-assistant/google/drive_client_secret.json
     ```
     (This directory is outside the repo, so the secret and token are never committed.)

4. **Authorize** — in a terminal at the repo root:
   ```
   python scripts/drive_upload.py auth
   ```
   It prints a URL; open it in any browser (on WSL, your Windows browser), sign in
   and approve. The browser then redirects to `localhost`, which reaches the script.
   On success it writes `~/.config/scherbring-assistant/google/drive_token.json`
   and prints `{"status": "authorized", ...}`.

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
