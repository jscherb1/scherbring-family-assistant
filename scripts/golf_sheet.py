"""Google Sheets side of the golf tracker: read the history, append new rounds.

Used only by scripts/golf.py (best-effort mirror) and `golf_store.py import --from-sheet`.
Auth reuses the Drive token from scripts/drive_upload.py; the `drive` scope is accepted by the
Sheets API, so no extra consent is needed, but the Google Sheets API must be enabled on the same
Cloud project. Columns on the tab: A Date, B Course, C Rating, D Slope, E Score, then formula
columns F-I (differential, flags, rank) that new rows inherit by copy-paste from the row above.
"""

from __future__ import annotations

from datetime import date


def _service():
    from googleapiclient.discovery import build

    import drive_upload
    creds = drive_upload._load_credentials(interactive=False)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _q(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def read_rows(cfg: dict) -> list[list]:
    """Data rows [date, course, rating, slope, score] from the tab (header skipped, blanks dropped)."""
    svc = _service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=cfg["sheet_id"], range=f"{_q(cfg['sheet_tab'])}!A2:E",
        valueRenderOption="FORMATTED_VALUE").execute()
    return [r for r in res.get("values", []) if len(r) >= 5 and all(str(c).strip() for c in r[:5])]


def _sheet_gid(svc, sheet_id: str, tab: str) -> int:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"tab {tab!r} not found in the sheet")


def append_round(r: dict, cfg: dict) -> None:
    """Write the round under the last used row and copy the formula columns down from the row above."""
    svc = _service()
    sid, tab = cfg["sheet_id"], cfg["sheet_tab"]
    col_a = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"{_q(tab)}!A:A").execute().get("values", [])
    last = max((i for i, row in enumerate(col_a, start=1) if row and str(row[0]).strip()), default=1)
    new = last + 1
    d = date.fromisoformat(r["played_on"])
    values = [[f"{d.month}/{d.day}/{d.year % 100:02d}", r["course"], r["rating"], r["slope"], r["score"]]]
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{_q(tab)}!A{new}:E{new}", valueInputOption="USER_ENTERED",
        body={"values": values}).execute()
    if last > 1:
        gid = _sheet_gid(svc, sid, tab)
        rng = lambda row: {"sheetId": gid, "startRowIndex": row - 1, "endRowIndex": row,  # noqa: E731
                           "startColumnIndex": 5, "endColumnIndex": 9}
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"copyPaste": {
            "source": rng(last), "destination": rng(new), "pasteType": "PASTE_FORMULA"}}]}).execute()
