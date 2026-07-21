#!/usr/bin/env python3
"""Lawn & garden care CLI for the lawn-garden subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). The `lawn-garden` subagent
uses this to track the Reinders 6-Step program, chemical/product inventory, plant
and mulch-bed locations, applied treatments (program rounds and ad-hoc spot
sprays), recurring weed/pest issues, and a small yard config (size, coordinates,
equipment) used by the weekly proactive check.

Usage:
    python scripts/lawn_garden_store.py plant add --name "hostas" --location "front bed by garage" \
        [--plant-type shrub] [--notes "..."]
    python scripts/lawn_garden_store.py plant update --id <id> [--name ...] [--plant-type ...] \
        [--location ...] [--notes ...]
    python scripts/lawn_garden_store.py plant list [--search "hosta"]

    python scripts/lawn_garden_store.py product add --name "T-Zone SE" --category herbicide \
        [--active-ingredient "Triclopyr/Sulfentrazone/2,4-D/Dicamba"] [--epa-reg-no "..."] \
        [--container-desc "1 quart concentrate"] [--in-stock 1] [--notes "..."]
    python scripts/lawn_garden_store.py product update --id <id> [--name ...] [--category ...] \
        [--active-ingredient ...] [--epa-reg-no ...] [--container-desc ...] [--in-stock 0|1] [--notes ...]
    python scripts/lawn_garden_store.py product list [--category herbicide] [--in-stock-only]

    python scripts/lawn_garden_store.py program seed
        (one-time insert of the 6 Reinders rounds; no-op if already seeded)
    python scripts/lawn_garden_store.py program list
    python scripts/lawn_garden_store.py program update --round-number 2 [--task-label ...] \
        [--product-name ...] [--timing-desc ...] [--timing-month ...] [--coverage-desc ...] [--notes ...]

    python scripts/lawn_garden_store.py treatment add --treatment-date 2026-07-20 \
        --product-name "T-Zone SE" --method spot-spray [--round-number 2] [--area "back fence line"] \
        [--target-weeds-json '["thistle"]'] [--notes "..."]
    python scripts/lawn_garden_store.py treatment list [--since 2026-01-01] [--until 2026-12-31] \
        [--round-number 2] [--method spot-spray] [--limit 20]
    python scripts/lawn_garden_store.py treatment last [--method spot-spray] [--round-number 2]
        (most recent matching treatment, or {} if none - used to compute days-since-last)

    python scripts/lawn_garden_store.py issue add --issue "thistle patch" --location "by the shed" \
        [--first-noted 2026-07-20] [--notes "..."]
    python scripts/lawn_garden_store.py issue update --id <id> [--status resolved] \
        [--resolved-date 2026-07-20] [--notes ...] [--location ...]
    python scripts/lawn_garden_store.py issue list [--status active]

    python scripts/lawn_garden_store.py config set --key yard_size_sqft --value 10000
    python scripts/lawn_garden_store.py config get --key yard_size_sqft
    python scripts/lawn_garden_store.py config list

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

REINDERS_PROGRAM = [
    dict(
        round_number=1,
        task_label="Pre-emerge fertilizer application",
        product_name="15-0-0 Bar. 80% RxN or 19-0-6 Dim. 50% RxN (50lb bag)",
        timing_desc="Mid April (when grass starts greening up)",
        timing_month=4,
        coverage_desc="12500 sq. ft.",
        notes=None,
    ),
    dict(
        round_number=2,
        task_label="Herbicide application",
        product_name="T-Zone",
        timing_desc="May (when weeds are actively growing)",
        timing_month=5,
        coverage_desc="1.5 oz per 1000 sq. ft. (1 gallon covers 2 acres)",
        notes=None,
    ),
    dict(
        round_number=3,
        task_label="Fertilizer application",
        product_name="33-0-5 100% RxN 2% Fe. (50lb bag)",
        timing_desc="Early June",
        timing_month=6,
        coverage_desc="16000 sq. ft.",
        notes=None,
    ),
    dict(
        round_number=4,
        task_label="Fertilizer application and spot spray weeds as needed",
        product_name="20-0-5 100% RxN 10% Milorganite",
        timing_desc="Early August",
        timing_month=8,
        coverage_desc="10000 sq. ft.",
        notes=None,
    ),
    dict(
        round_number=5,
        task_label="Herbicide application",
        product_name="T-Zone",
        timing_desc="Late August",
        timing_month=8,
        coverage_desc="1.5 oz per 1000 sq. ft. (1 gallon covers 2 acres)",
        notes=None,
    ),
    dict(
        round_number=6,
        task_label="Fertilizer application",
        product_name="21-0-21 75% RxN 2% Fe. (50lb bag)",
        timing_desc="Mid October",
        timing_month=10,
        coverage_desc="10500 sq. ft.",
        notes=None,
    ),
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _local_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _require_date(value: str, field: str) -> int:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"error": f"invalid {field} {value!r}, expected YYYY-MM-DD"}), file=sys.stderr)
        return 2
    return 0


def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict:
    d = dict(row)
    for key in json_fields:
        if d.get(key):
            d[key] = json.loads(d[key])
    return d


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# plant
# ---------------------------------------------------------------------------


def cmd_plant_add(args: argparse.Namespace) -> int:
    now = _local_now_iso()
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO lawn_garden_plants (id, name, plant_type, location, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, args.name, args.plant_type, args.location, args.notes, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM lawn_garden_plants WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row))
    return 0


def cmd_plant_update(args: argparse.Namespace) -> int:
    fields, params = [], []
    for col, val in (("name", args.name), ("plant_type", args.plant_type), ("location", args.location), ("notes", args.notes)):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        return 2
    fields.append("updated_at = ?")
    params.append(_local_now_iso())
    conn = _connect()
    try:
        params.append(args.id)
        cur = conn.execute(f"UPDATE lawn_garden_plants SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no plant with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM lawn_garden_plants WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row))
    return 0


def cmd_plant_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM lawn_garden_plants WHERE 1=1"
        params: list = []
        if args.search:
            like = f"%{args.search}%"
            sql += " AND (name LIKE ? OR location LIKE ? OR notes LIKE ?)"
            params.extend([like, like, like])
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([_row_to_dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# product
# ---------------------------------------------------------------------------


def cmd_product_add(args: argparse.Namespace) -> int:
    now = _local_now_iso()
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO lawn_garden_products (
                id, name, category, active_ingredient, epa_reg_no, container_desc,
                in_stock, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id, args.name, args.category, args.active_ingredient, args.epa_reg_no,
                args.container_desc, args.in_stock, args.notes, now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM lawn_garden_products WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row))
    return 0


def cmd_product_update(args: argparse.Namespace) -> int:
    fields, params = [], []
    for col, val in (
        ("name", args.name), ("category", args.category), ("active_ingredient", args.active_ingredient),
        ("epa_reg_no", args.epa_reg_no), ("container_desc", args.container_desc),
        ("in_stock", args.in_stock), ("notes", args.notes),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        return 2
    fields.append("updated_at = ?")
    params.append(_local_now_iso())
    conn = _connect()
    try:
        params.append(args.id)
        cur = conn.execute(f"UPDATE lawn_garden_products SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no product with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM lawn_garden_products WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row))
    return 0


def cmd_product_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM lawn_garden_products WHERE 1=1"
        params: list = []
        if args.category:
            sql += " AND category = ?"
            params.append(args.category)
        if args.in_stock_only:
            sql += " AND in_stock = 1"
        sql += " ORDER BY category, name"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([_row_to_dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# program
# ---------------------------------------------------------------------------


def cmd_program_seed(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        existing = conn.execute("SELECT COUNT(*) AS n FROM lawn_garden_program").fetchone()["n"]
        if existing:
            rows = conn.execute("SELECT * FROM lawn_garden_program ORDER BY round_number").fetchall()
            _print({"seeded": False, "reason": "already seeded", "rounds": [dict(r) for r in rows]})
            return 0
        for round_data in REINDERS_PROGRAM:
            conn.execute(
                """
                INSERT INTO lawn_garden_program (
                    id, round_number, task_label, product_name, timing_desc, timing_month, coverage_desc, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), round_data["round_number"], round_data["task_label"],
                    round_data["product_name"], round_data["timing_desc"], round_data["timing_month"],
                    round_data["coverage_desc"], round_data["notes"],
                ),
            )
        conn.commit()
        rows = conn.execute("SELECT * FROM lawn_garden_program ORDER BY round_number").fetchall()
    finally:
        conn.close()
    _print({"seeded": True, "rounds": [dict(r) for r in rows]})
    return 0


def cmd_program_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM lawn_garden_program ORDER BY round_number").fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def cmd_program_update(args: argparse.Namespace) -> int:
    fields, params = [], []
    for col, val in (
        ("task_label", args.task_label), ("product_name", args.product_name),
        ("timing_desc", args.timing_desc), ("timing_month", args.timing_month),
        ("coverage_desc", args.coverage_desc), ("notes", args.notes),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        return 2
    conn = _connect()
    try:
        params.append(args.round_number)
        cur = conn.execute(f"UPDATE lawn_garden_program SET {', '.join(fields)} WHERE round_number = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no program round {args.round_number}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM lawn_garden_program WHERE round_number = ?", (args.round_number,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


# ---------------------------------------------------------------------------
# treatment
# ---------------------------------------------------------------------------


def cmd_treatment_add(args: argparse.Namespace) -> int:
    treatment_date = args.treatment_date or _today()
    rc = _require_date(treatment_date, "--treatment-date")
    if rc:
        return rc
    if args.target_weeds_json is not None:
        try:
            json.loads(args.target_weeds_json)
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"invalid --target-weeds-json: {exc}"}), file=sys.stderr)
            return 2
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO lawn_garden_treatments (
                id, treatment_date, round_number, product_name, method, area,
                target_weeds_json, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id, treatment_date, args.round_number, args.product_name, args.method,
                args.area, args.target_weeds_json, args.notes, _local_now_iso(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM lawn_garden_treatments WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row, ("target_weeds_json",)))
    return 0


def cmd_treatment_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM lawn_garden_treatments WHERE 1=1"
        params: list = []
        if args.since:
            sql += " AND treatment_date >= ?"
            params.append(args.since)
        if args.until:
            sql += " AND treatment_date <= ?"
            params.append(args.until)
        if args.round_number is not None:
            sql += " AND round_number = ?"
            params.append(args.round_number)
        if args.method:
            sql += " AND method = ?"
            params.append(args.method)
        sql += " ORDER BY treatment_date DESC, created_at DESC"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([_row_to_dict(r, ("target_weeds_json",)) for r in rows])
    return 0


def cmd_treatment_last(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM lawn_garden_treatments WHERE 1=1"
        params: list = []
        if args.method:
            sql += " AND method = ?"
            params.append(args.method)
        if args.round_number is not None:
            sql += " AND round_number = ?"
            params.append(args.round_number)
        sql += " ORDER BY treatment_date DESC, created_at DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row, ("target_weeds_json",)) if row else {})
    return 0


# ---------------------------------------------------------------------------
# issue
# ---------------------------------------------------------------------------


def cmd_issue_add(args: argparse.Namespace) -> int:
    now = _local_now_iso()
    first_noted = args.first_noted or _today()
    rc = _require_date(first_noted, "--first-noted")
    if rc:
        return rc
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO lawn_garden_issues (
                id, issue, location, status, first_noted, resolved_date, notes, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, NULL, ?, ?, ?)
            """,
            (new_id, args.issue, args.location, first_noted, args.notes, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM lawn_garden_issues WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_issue_update(args: argparse.Namespace) -> int:
    fields, params = [], []
    for col, val in (
        ("location", args.location), ("status", args.status),
        ("resolved_date", args.resolved_date), ("notes", args.notes),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        return 2
    fields.append("updated_at = ?")
    params.append(_local_now_iso())
    conn = _connect()
    try:
        params.append(args.id)
        cur = conn.execute(f"UPDATE lawn_garden_issues SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no issue with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM lawn_garden_issues WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_issue_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM lawn_garden_issues WHERE 1=1"
        params: list = []
        if args.status:
            sql += " AND status = ?"
            params.append(args.status)
        sql += " ORDER BY first_noted DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def cmd_config_set(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO lawn_garden_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (args.key, args.value),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM lawn_garden_config WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM lawn_garden_config WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        print(json.dumps({"error": f"no config key {args.key}"}), file=sys.stderr)
        return 1
    _print(dict(row))
    return 0


def cmd_config_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM lawn_garden_config ORDER BY key").fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lawn & garden care store.")
    sub = parser.add_subparsers(dest="command", required=True)

    plant = sub.add_parser("plant", help="Plant/bed location tracking.")
    plant_sub = plant.add_subparsers(dest="subcommand", required=True)

    pa = plant_sub.add_parser("add")
    pa.add_argument("--name", required=True)
    pa.add_argument("--location", required=True)
    pa.add_argument("--plant-type", dest="plant_type", default=None)
    pa.add_argument("--notes", default=None)
    pa.set_defaults(func=cmd_plant_add)

    pu = plant_sub.add_parser("update")
    pu.add_argument("--id", required=True)
    pu.add_argument("--name", default=None)
    pu.add_argument("--location", default=None)
    pu.add_argument("--plant-type", dest="plant_type", default=None)
    pu.add_argument("--notes", default=None)
    pu.set_defaults(func=cmd_plant_update)

    pl = plant_sub.add_parser("list")
    pl.add_argument("--search", default=None)
    pl.set_defaults(func=cmd_plant_list)

    product = sub.add_parser("product", help="Chemical/product inventory.")
    product_sub = product.add_subparsers(dest="subcommand", required=True)

    proda = product_sub.add_parser("add")
    proda.add_argument("--name", required=True)
    proda.add_argument("--category", required=True, choices=["fertilizer", "herbicide", "other"])
    proda.add_argument("--active-ingredient", dest="active_ingredient", default=None)
    proda.add_argument("--epa-reg-no", dest="epa_reg_no", default=None)
    proda.add_argument("--container-desc", dest="container_desc", default=None)
    proda.add_argument("--in-stock", dest="in_stock", type=int, default=1, choices=[0, 1])
    proda.add_argument("--notes", default=None)
    proda.set_defaults(func=cmd_product_add)

    produ = product_sub.add_parser("update")
    produ.add_argument("--id", required=True)
    produ.add_argument("--name", default=None)
    produ.add_argument("--category", default=None, choices=["fertilizer", "herbicide", "other"])
    produ.add_argument("--active-ingredient", dest="active_ingredient", default=None)
    produ.add_argument("--epa-reg-no", dest="epa_reg_no", default=None)
    produ.add_argument("--container-desc", dest="container_desc", default=None)
    produ.add_argument("--in-stock", dest="in_stock", type=int, default=None, choices=[0, 1])
    produ.add_argument("--notes", default=None)
    produ.set_defaults(func=cmd_product_update)

    prodl = product_sub.add_parser("list")
    prodl.add_argument("--category", default=None, choices=["fertilizer", "herbicide", "other"])
    prodl.add_argument("--in-stock-only", dest="in_stock_only", action="store_true")
    prodl.set_defaults(func=cmd_product_list)

    program = sub.add_parser("program", help="Reinders 6-Step program reference.")
    program_sub = program.add_subparsers(dest="subcommand", required=True)

    progs = program_sub.add_parser("seed")
    progs.set_defaults(func=cmd_program_seed)

    progl = program_sub.add_parser("list")
    progl.set_defaults(func=cmd_program_list)

    progu = program_sub.add_parser("update")
    progu.add_argument("--round-number", dest="round_number", type=int, required=True)
    progu.add_argument("--task-label", dest="task_label", default=None)
    progu.add_argument("--product-name", dest="product_name", default=None)
    progu.add_argument("--timing-desc", dest="timing_desc", default=None)
    progu.add_argument("--timing-month", dest="timing_month", type=int, default=None)
    progu.add_argument("--coverage-desc", dest="coverage_desc", default=None)
    progu.add_argument("--notes", default=None)
    progu.set_defaults(func=cmd_program_update)

    treatment = sub.add_parser("treatment", help="Applied treatments log.")
    treatment_sub = treatment.add_subparsers(dest="subcommand", required=True)

    ta = treatment_sub.add_parser("add")
    ta.add_argument("--treatment-date", dest="treatment_date", default=None, help="YYYY-MM-DD; defaults to today.")
    ta.add_argument("--product-name", dest="product_name", required=True)
    ta.add_argument("--method", required=True, choices=["broadcast", "spot-spray", "backpack"])
    ta.add_argument("--round-number", dest="round_number", type=int, default=None)
    ta.add_argument("--area", default=None)
    ta.add_argument("--target-weeds-json", dest="target_weeds_json", default=None)
    ta.add_argument("--notes", default=None)
    ta.set_defaults(func=cmd_treatment_add)

    tl = treatment_sub.add_parser("list")
    tl.add_argument("--since", default=None)
    tl.add_argument("--until", default=None)
    tl.add_argument("--round-number", dest="round_number", type=int, default=None)
    tl.add_argument("--method", default=None, choices=["broadcast", "spot-spray", "backpack"])
    tl.add_argument("--limit", type=int, default=None)
    tl.set_defaults(func=cmd_treatment_list)

    tlast = treatment_sub.add_parser("last")
    tlast.add_argument("--method", default=None, choices=["broadcast", "spot-spray", "backpack"])
    tlast.add_argument("--round-number", dest="round_number", type=int, default=None)
    tlast.set_defaults(func=cmd_treatment_last)

    issue = sub.add_parser("issue", help="Recurring weed/pest/disease issues.")
    issue_sub = issue.add_subparsers(dest="subcommand", required=True)

    ia = issue_sub.add_parser("add")
    ia.add_argument("--issue", required=True)
    ia.add_argument("--location", default=None)
    ia.add_argument("--first-noted", dest="first_noted", default=None, help="YYYY-MM-DD; defaults to today.")
    ia.add_argument("--notes", default=None)
    ia.set_defaults(func=cmd_issue_add)

    iu = issue_sub.add_parser("update")
    iu.add_argument("--id", required=True)
    iu.add_argument("--location", default=None)
    iu.add_argument("--status", default=None, choices=["active", "resolved"])
    iu.add_argument("--resolved-date", dest="resolved_date", default=None)
    iu.add_argument("--notes", default=None)
    iu.set_defaults(func=cmd_issue_update)

    il = issue_sub.add_parser("list")
    il.add_argument("--status", default=None, choices=["active", "resolved"])
    il.set_defaults(func=cmd_issue_list)

    config = sub.add_parser("config", help="Yard profile key/value config.")
    config_sub = config.add_subparsers(dest="subcommand", required=True)

    cs = config_sub.add_parser("set")
    cs.add_argument("--key", required=True)
    cs.add_argument("--value", required=True)
    cs.set_defaults(func=cmd_config_set)

    cg = config_sub.add_parser("get")
    cg.add_argument("--key", required=True)
    cg.set_defaults(func=cmd_config_get)

    cl = config_sub.add_parser("list")
    cl.set_defaults(func=cmd_config_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
