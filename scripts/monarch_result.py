#!/usr/bin/env python3
"""Read-only queries over large saved Monarch tool results.

Claude Code saves any MCP result over a size limit to a file instead of putting
it in context (a cashflow call is ~340 KB, budgets ~280 KB). Agents that need a
figure from one of those files use this instead of an ad hoc `python -c` or
`jq`, neither of which headless runs are permitted to use.

The file is JSON, usually `{"result": "<json string>"}`; both are handled. Paths
are dotted, with list indices as numbers: `summary.0.summary.savings`.

Subcommands (all print one small JSON document):
    shape FILE [--depth 3]                    structure: keys, types, list lengths
    get FILE PATH [PATH ...]                  value(s) at each dotted path
    sum FILE --path data --field amount [--where direction=expense] [--sign neg|pos]
                                              count and sum over a list of objects
    group FILE --path . --key name --fields planned,actual,remaining [--top 20]
                                              group a list by a key and sum fields
    top FILE --path by_category --sort sum [--n 15] [--fields category,sum] [--asc]
                                              the top N list rows, chosen fields only

Only files inside Claude Code's tool-results folders, this repo's state/, or
/tmp/claude-* can be read, so this cannot be used to read credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from paths import REPO_ROOT

MAX_OUT_CHARS = 8000


def _allowed(path: Path) -> bool:
    resolved = path.resolve()
    projects = (Path.home() / ".claude" / "projects").resolve()
    state = (REPO_ROOT / "state").resolve()
    in_results = projects in resolved.parents and "tool-results" in resolved.parts
    in_state = state in resolved.parents
    in_tmp = Path("/tmp") in resolved.parents and any(p.startswith("claude-") for p in resolved.parts)
    return in_results or in_state or in_tmp


def load(file: str) -> Any:
    path = Path(file).expanduser()
    if not _allowed(path):
        raise PermissionError(
            "only saved tool results, this repo's state/ and /tmp/claude-* files may be read"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], str):
        try:
            return json.loads(data["result"])
        except ValueError:
            return data["result"]
    return data


def walk(data: Any, path: str) -> Any:
    """Follow a dotted path; '.' or '' means the root."""
    cur = data
    for part in [p for p in path.split(".") if p] if path not in (".", "") else []:
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"no key {part!r}; available: {sorted(cur)[:15]}")
            cur = cur[part]
        else:
            raise KeyError(f"cannot descend into {type(cur).__name__} at {part!r}")
    return cur


def shape(obj: Any, depth: int) -> Any:
    if isinstance(obj, dict):
        if depth <= 0:
            return f"dict[{len(obj)} keys]"
        return {k: shape(v, depth - 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        first = shape(obj[0], depth - 1) if obj and depth > 0 else None
        return {"list": len(obj), "first": first}
    return type(obj).__name__


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _rows(data: Any, path: str) -> list[dict]:
    rows = walk(data, path)
    if not isinstance(rows, list):
        raise ValueError(f"path {path!r} is a {type(rows).__name__}, not a list")
    return [r for r in rows if isinstance(r, dict)]


def _matches(row: dict, wheres: list[str]) -> bool:
    for w in wheres:
        k, _, v = w.partition("=")
        if str(row.get(k)) != v:
            return False
    return True


def run(args: argparse.Namespace) -> Any:
    data = load(args.file)
    if args.cmd == "shape":
        return shape(data, args.depth)
    if args.cmd == "get":
        return {p: walk(data, p) for p in args.paths}
    if args.cmd == "sum":
        rows = [r for r in _rows(data, args.path) if _matches(r, args.where or [])]
        vals = [_num(r.get(args.field)) for r in rows]
        if args.sign == "neg":
            vals = [v for v in vals if v < 0]
        elif args.sign == "pos":
            vals = [v for v in vals if v > 0]
        return {"rows": len(rows), "counted": len(vals), "sum": round(sum(vals), 2)}
    if args.cmd == "group":
        fields = [f for f in args.fields.split(",") if f]
        groups: dict[str, dict[str, float]] = {}
        for r in _rows(data, args.path):
            g = groups.setdefault(str(r.get(args.key)), {f: 0.0 for f in fields})
            for f in fields:
                g[f] += _num(r.get(f))
        ordered = sorted(groups.items(), key=lambda kv: -abs(kv[1][fields[0]]) if fields else 0)
        out = [{args.key: k, **{f: round(v[f], 2) for f in fields}} for k, v in ordered]
        return {"groups": len(out), "rows": out[: args.top] if args.top else out}
    if args.cmd == "top":
        rows = _rows(data, args.path)
        rows.sort(key=lambda r: _num(r.get(args.sort)), reverse=not args.asc)
        fields = [f for f in (args.fields or "").split(",") if f]
        picked = [{f: r.get(f) for f in fields} if fields else r for r in rows[: args.n]]
        return {"total_rows": len(rows), "rows": picked}
    raise ValueError(args.cmd)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("shape")
    p.add_argument("file")
    p.add_argument("--depth", type=int, default=3)
    p = sub.add_parser("get")
    p.add_argument("file")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("sum")
    p.add_argument("file")
    p.add_argument("--path", default="data")
    p.add_argument("--field", default="amount")
    p.add_argument("--where", action="append", help="FIELD=VALUE row filter (repeatable)")
    p.add_argument("--sign", choices=["neg", "pos"], help="only negative or only positive values")
    p = sub.add_parser("group")
    p.add_argument("file")
    p.add_argument("--path", default=".")
    p.add_argument("--key", required=True)
    p.add_argument("--fields", required=True, help="comma-separated numeric fields to sum")
    p.add_argument("--top", type=int, default=0)
    p = sub.add_parser("top")
    p.add_argument("file")
    p.add_argument("--path", required=True)
    p.add_argument("--sort", required=True)
    p.add_argument("--n", type=int, default=15)
    p.add_argument("--fields", help="comma-separated fields to keep")
    p.add_argument("--asc", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = json.dumps(run(args), ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - report as data, never a traceback
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    if len(out) > MAX_OUT_CHARS:
        out = out[:MAX_OUT_CHARS] + f'... [truncated at {MAX_OUT_CHARS} chars; narrow the query with --top/--fields]'
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
