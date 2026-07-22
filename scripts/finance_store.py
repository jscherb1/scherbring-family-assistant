#!/usr/bin/env python3
"""Personal finance CLI for the `finance` subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). This is NOT where Monarch
Money data lives — Monarch (via the local `monarch` MCP server) is the source of
truth for accounts/transactions/tags/rules. This store only tracks the finance
agent's own working state:

  - `who-map`     learned merchant/account -> WHO-tag signal, with confidence
                  (mirrors scripts/hyvee_item_prefs' learned-map pattern)
  - `log`         append-only audit of every tagging decision (mirrors
                  hyvee_feedback_log)
  - `rule-proposal` proposed Monarch auto-tagging rules awaiting user approval
  - `config`      free-form key/value (Drive folder id, WHO tag id cache,
                  historical-sweep cursor for resumability)
  - `report`      history of generated spending-summary reports (Phase 2,
                  finance-reporter subagent) — period, date range, Drive link

Usage:
    python scripts/finance_store.py who-map set --signal-key "costco" \
        --signal-type merchant --who-tag "WHO:Justin" --confidence 0.9 \
        [--source inferred|user]
    python scripts/finance_store.py who-map get --signal-key "costco"
    python scripts/finance_store.py who-map list [--signal-type merchant] [--min-confidence 0.7]
    python scripts/finance_store.py who-map feedback --signal-key "costco" --result confirmed|rejected
        (bumps times_confirmed/times_rejected and nudges confidence)

    python scripts/finance_store.py log add --transaction-id <id> \
        --action tagged_auto|tagged_confirmed|skipped_ambiguous|marked_reviewed \
        [--merchant-name "..."] [--account-id <id>] [--amount -42.10] \
        [--txn-date 2026-07-20] [--proposed-who-tag "WHO:Justin"] [--confidence 0.9] \
        [--user-decision confirmed|corrected|rejected] [--note "..."]
    python scripts/finance_store.py log list [--transaction-id <id>] [--action tagged_auto] \
        [--since 2026-07-01] [--limit 50]

    python scripts/finance_store.py rule-proposal add --merchant-name "Costco" \
        --who-tag "WHO:Justin" [--evidence-json '["txn_id_1","txn_id_2"]']
    python scripts/finance_store.py rule-proposal list [--status proposed]
    python scripts/finance_store.py rule-proposal update --id <id> \
        --status approved|rejected|created [--monarch-rule-id <id>]

    python scripts/finance_store.py report add --period weekly|monthly|annual \
        --range-start 2026-07-13 --range-end 2026-07-19 \
        [--drive-file-id <id>] [--drive-url "..."] [--summary "..."]
    python scripts/finance_store.py report list [--period weekly|monthly|annual] [--limit 10]
    python scripts/finance_store.py report update --id <id> \
        [--drive-file-id <id>] [--drive-url "..."] [--summary "..."]

    python scripts/finance_store.py config set --key drive_folder_id --value "1He_..."
    python scripts/finance_store.py config get --key drive_folder_id
    python scripts/finance_store.py config list

All commands print JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _local_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _err(msg: str) -> int:
    print(json.dumps({"error": msg}), file=sys.stderr)
    return 2


# --- who-map ---------------------------------------------------------------

def cmd_who_map_set(args: argparse.Namespace) -> int:
    if args.signal_type not in ("merchant", "account"):
        return _err("--signal-type must be 'merchant' or 'account'")
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO finance_who_map (signal_key, signal_type, who_tag_name, confidence, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_key) DO UPDATE SET
                signal_type = excluded.signal_type,
                who_tag_name = excluded.who_tag_name,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (args.signal_key, args.signal_type, args.who_tag, args.confidence, args.source, _local_now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM finance_who_map WHERE signal_key = ?", (args.signal_key,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_who_map_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM finance_who_map WHERE signal_key = ?", (args.signal_key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _err(f"no who-map entry for signal_key {args.signal_key!r}")
    _print(dict(row))
    return 0


def cmd_who_map_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM finance_who_map WHERE 1=1"
        params: list = []
        if args.signal_type:
            sql += " AND signal_type = ?"
            params.append(args.signal_type)
        if args.min_confidence is not None:
            sql += " AND confidence >= ?"
            params.append(args.min_confidence)
        sql += " ORDER BY updated_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def cmd_who_map_feedback(args: argparse.Namespace) -> int:
    if args.result not in ("confirmed", "rejected"):
        return _err("--result must be 'confirmed' or 'rejected'")
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM finance_who_map WHERE signal_key = ?", (args.signal_key,)).fetchone()
        if row is None:
            return _err(f"no who-map entry for signal_key {args.signal_key!r}")
        confidence = row["confidence"]
        times_confirmed = row["times_confirmed"]
        times_rejected = row["times_rejected"]
        if args.result == "confirmed":
            times_confirmed += 1
            confidence = min(1.0, confidence + 0.1)
        else:
            times_rejected += 1
            confidence = max(0.0, confidence - 0.2)
        conn.execute(
            """
            UPDATE finance_who_map
            SET times_confirmed = ?, times_rejected = ?, confidence = ?, updated_at = ?
            WHERE signal_key = ?
            """,
            (times_confirmed, times_rejected, confidence, _local_now_iso(), args.signal_key),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM finance_who_map WHERE signal_key = ?", (args.signal_key,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


# --- log ---------------------------------------------------------------

def cmd_log_add(args: argparse.Namespace) -> int:
    valid_actions = ("tagged_auto", "tagged_confirmed", "skipped_ambiguous", "marked_reviewed")
    if args.action not in valid_actions:
        return _err(f"--action must be one of {valid_actions}")
    new_id = uuid.uuid4().hex
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO finance_tag_log (
                id, ts, transaction_id, merchant_name, account_id, amount, txn_date,
                proposed_who_tag, confidence, action, user_decision, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id, _local_now_iso(), args.transaction_id, args.merchant_name, args.account_id,
                args.amount, args.txn_date, args.proposed_who_tag, args.confidence, args.action,
                args.user_decision, args.note,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM finance_tag_log WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_log_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM finance_tag_log WHERE 1=1"
        params: list = []
        if args.transaction_id:
            sql += " AND transaction_id = ?"
            params.append(args.transaction_id)
        if args.action:
            sql += " AND action = ?"
            params.append(args.action)
        if args.since:
            sql += " AND ts >= ?"
            params.append(args.since)
        sql += " ORDER BY ts DESC"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# --- rule-proposal ---------------------------------------------------------------

def cmd_rule_proposal_add(args: argparse.Namespace) -> int:
    new_id = uuid.uuid4().hex
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO finance_rule_proposals (
                id, created_at, merchant_name, who_tag_name, evidence_json, status
            ) VALUES (?, ?, ?, ?, ?, 'proposed')
            """,
            (new_id, _local_now_iso(), args.merchant_name, args.who_tag, args.evidence_json),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM finance_rule_proposals WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_rule_proposal_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM finance_rule_proposals WHERE 1=1"
        params: list = []
        if args.status:
            sql += " AND status = ?"
            params.append(args.status)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def cmd_rule_proposal_update(args: argparse.Namespace) -> int:
    valid_statuses = ("proposed", "approved", "rejected", "created")
    if args.status not in valid_statuses:
        return _err(f"--status must be one of {valid_statuses}")
    conn = _connect()
    try:
        cur = conn.execute(
            """
            UPDATE finance_rule_proposals
            SET status = ?, monarch_rule_id = COALESCE(?, monarch_rule_id), decided_at = ?
            WHERE id = ?
            """,
            (args.status, args.monarch_rule_id, _local_now_iso(), args.id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return _err(f"no rule proposal with id {args.id}")
        row = conn.execute("SELECT * FROM finance_rule_proposals WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


# --- report ---------------------------------------------------------------

def cmd_report_add(args: argparse.Namespace) -> int:
    if args.period not in ("weekly", "monthly", "annual"):
        return _err("--period must be 'weekly', 'monthly', or 'annual'")
    new_id = uuid.uuid4().hex
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO finance_report_log (
                id, created_at, period, range_start, range_end,
                drive_file_id, drive_url, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id, _local_now_iso(), args.period, args.range_start, args.range_end,
                args.drive_file_id, args.drive_url, args.summary,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM finance_report_log WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_report_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM finance_report_log WHERE 1=1"
        params: list = []
        if args.period:
            sql += " AND period = ?"
            params.append(args.period)
        sql += " ORDER BY range_end DESC"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def cmd_report_update(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            """
            UPDATE finance_report_log
            SET drive_file_id = COALESCE(?, drive_file_id),
                drive_url = COALESCE(?, drive_url),
                summary = COALESCE(?, summary)
            WHERE id = ?
            """,
            (args.drive_file_id, args.drive_url, args.summary, args.id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return _err(f"no report with id {args.id}")
        row = conn.execute("SELECT * FROM finance_report_log WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


# --- config ---------------------------------------------------------------

def cmd_config_set(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO finance_config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (args.key, args.value),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM finance_config WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM finance_config WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _err(f"no config value for key {args.key!r}")
    _print(dict(row))
    return 0


def cmd_config_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM finance_config ORDER BY key").fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal finance agent working-state store.")
    sub = parser.add_subparsers(dest="command", required=True)

    who_map = sub.add_parser("who-map", help="Learned merchant/account -> WHO-tag map.")
    who_map_sub = who_map.add_subparsers(dest="who_map_command", required=True)

    wm_set = who_map_sub.add_parser("set", help="Create/update a who-map entry.")
    wm_set.add_argument("--signal-key", required=True)
    wm_set.add_argument("--signal-type", required=True, choices=["merchant", "account"])
    wm_set.add_argument("--who-tag", dest="who_tag", required=True)
    wm_set.add_argument("--confidence", type=float, required=True)
    wm_set.add_argument("--source", default="inferred", choices=["inferred", "user"])
    wm_set.set_defaults(func=cmd_who_map_set)

    wm_get = who_map_sub.add_parser("get", help="Get one who-map entry.")
    wm_get.add_argument("--signal-key", required=True)
    wm_get.set_defaults(func=cmd_who_map_get)

    wm_list = who_map_sub.add_parser("list", help="List who-map entries.")
    wm_list.add_argument("--signal-type", default=None, choices=["merchant", "account"])
    wm_list.add_argument("--min-confidence", dest="min_confidence", type=float, default=None)
    wm_list.set_defaults(func=cmd_who_map_list)

    wm_fb = who_map_sub.add_parser("feedback", help="Record confirm/reject feedback, adjusting confidence.")
    wm_fb.add_argument("--signal-key", required=True)
    wm_fb.add_argument("--result", required=True, choices=["confirmed", "rejected"])
    wm_fb.set_defaults(func=cmd_who_map_feedback)

    log = sub.add_parser("log", help="Append-only tagging-decision audit log.")
    log_sub = log.add_subparsers(dest="log_command", required=True)

    log_add = log_sub.add_parser("add", help="Record a tagging decision.")
    log_add.add_argument("--transaction-id", required=True)
    log_add.add_argument("--action", required=True, choices=["tagged_auto", "tagged_confirmed", "skipped_ambiguous", "marked_reviewed"])
    log_add.add_argument("--merchant-name", default=None)
    log_add.add_argument("--account-id", default=None)
    log_add.add_argument("--amount", type=float, default=None)
    log_add.add_argument("--txn-date", dest="txn_date", default=None)
    log_add.add_argument("--proposed-who-tag", dest="proposed_who_tag", default=None)
    log_add.add_argument("--confidence", type=float, default=None)
    log_add.add_argument("--user-decision", dest="user_decision", default=None, choices=["confirmed", "corrected", "rejected"])
    log_add.add_argument("--note", default=None)
    log_add.set_defaults(func=cmd_log_add)

    log_list = log_sub.add_parser("list", help="List tagging-decision log entries.")
    log_list.add_argument("--transaction-id", default=None)
    log_list.add_argument("--action", default=None, choices=["tagged_auto", "tagged_confirmed", "skipped_ambiguous", "marked_reviewed"])
    log_list.add_argument("--since", default=None, help="ISO timestamp/date; ts >= this.")
    log_list.add_argument("--limit", type=int, default=None)
    log_list.set_defaults(func=cmd_log_list)

    rp = sub.add_parser("rule-proposal", help="Proposed Monarch auto-tagging rules.")
    rp_sub = rp.add_subparsers(dest="rule_proposal_command", required=True)

    rp_add = rp_sub.add_parser("add", help="Propose a new WHO-tag rule.")
    rp_add.add_argument("--merchant-name", required=True)
    rp_add.add_argument("--who-tag", dest="who_tag", required=True)
    rp_add.add_argument("--evidence-json", dest="evidence_json", default=None)
    rp_add.set_defaults(func=cmd_rule_proposal_add)

    rp_list = rp_sub.add_parser("list", help="List proposed rules.")
    rp_list.add_argument("--status", default=None, choices=["proposed", "approved", "rejected", "created"])
    rp_list.set_defaults(func=cmd_rule_proposal_list)

    rp_upd = rp_sub.add_parser("update", help="Change a proposal's status.")
    rp_upd.add_argument("--id", required=True)
    rp_upd.add_argument("--status", required=True, choices=["proposed", "approved", "rejected", "created"])
    rp_upd.add_argument("--monarch-rule-id", dest="monarch_rule_id", default=None)
    rp_upd.set_defaults(func=cmd_rule_proposal_update)

    rpt = sub.add_parser("report", help="History of generated spending-summary reports.")
    rpt_sub = rpt.add_subparsers(dest="report_command", required=True)

    rpt_add = rpt_sub.add_parser("add", help="Record a generated report.")
    rpt_add.add_argument("--period", required=True, choices=["weekly", "monthly", "annual"])
    rpt_add.add_argument("--range-start", dest="range_start", required=True)
    rpt_add.add_argument("--range-end", dest="range_end", required=True)
    rpt_add.add_argument("--drive-file-id", dest="drive_file_id", default=None)
    rpt_add.add_argument("--drive-url", dest="drive_url", default=None)
    rpt_add.add_argument("--summary", default=None)
    rpt_add.set_defaults(func=cmd_report_add)

    rpt_list = rpt_sub.add_parser("list", help="List generated reports.")
    rpt_list.add_argument("--period", default=None, choices=["weekly", "monthly", "annual"])
    rpt_list.add_argument("--limit", type=int, default=None)
    rpt_list.set_defaults(func=cmd_report_list)

    rpt_upd = rpt_sub.add_parser("update", help="Update a report's Drive fields/summary.")
    rpt_upd.add_argument("--id", required=True)
    rpt_upd.add_argument("--drive-file-id", dest="drive_file_id", default=None)
    rpt_upd.add_argument("--drive-url", dest="drive_url", default=None)
    rpt_upd.add_argument("--summary", default=None)
    rpt_upd.set_defaults(func=cmd_report_update)

    cfg = sub.add_parser("config", help="Free-form key/value config.")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)

    cfg_set = cfg_sub.add_parser("set", help="Set a config value.")
    cfg_set.add_argument("--key", required=True)
    cfg_set.add_argument("--value", required=True)
    cfg_set.set_defaults(func=cmd_config_set)

    cfg_get = cfg_sub.add_parser("get", help="Get a config value.")
    cfg_get.add_argument("--key", required=True)
    cfg_get.set_defaults(func=cmd_config_get)

    cfg_list = cfg_sub.add_parser("list", help="List all config values.")
    cfg_list.set_defaults(func=cmd_config_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
