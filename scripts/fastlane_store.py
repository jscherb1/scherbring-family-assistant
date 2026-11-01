#!/usr/bin/env python3
"""CLI for the fast lane (scripts/fastlane.py): try a message without running anything. JSON on stdout, errors on stderr.

    fastlane_store.py test "<text>" [--tier0-only] [--db PATH] [--today YYYY-MM-DD]
        Dry run: what the fast lane would do with this message (Tier 0 rules, then the Tier 1 model unless
        --tier0-only). Never writes, never touches a device or Todoist. Exit 0 = the fast lane would take it,
        1 = it would pass it to the agent, 2 = bad input.
    fastlane_store.py eval --file PATH [--tier1-sample N] [--show] [--tier0-only] [--db PATH]
        Replay a message history (JSONL of agent_stream turn_start events or {"text": ...}, or one message per line)
        and report coverage. Prints aggregates and the index of every message the fast lane would take; --show also
        prints those texts (for review: do not commit them).

Switching the lane on and off is `agent_ctl.py fastlane on|off|status`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fastlane  # noqa: E402


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _static_reason(text: str) -> str | None:
    """The eligibility rules that need only the text (the live ones also look at the row and the inbox)."""
    if not text.strip():
        return "empty"
    if len(text) > fastlane.MAX_TEXT:
        return "long"
    if fastlane.unsafe_text(text) and not fastlane._STATE_QUESTION.match(text.strip()):
        return "unsafe"
    if fastlane.meta_text(text):
        return "meta"
    return None


async def classify(text: str, ctx: fastlane.Context, router=None) -> dict:
    """Dry-run triage of one message: {"handled", "tier", "action", "fields", "reason", "would_reply", ...}."""
    reason = _static_reason(text)
    if reason:
        return {"handled": False, "reason": reason}
    match = fastlane.tier0(text, ctx)
    info: dict = {}
    if match is None:
        if router is None or not fastlane.worth_model(text, ctx):
            return {"handled": False, "reason": "no_match"}
        res = await router.route(text, ctx)
        info = {"model_latency_s": round(res.latency_s, 2), "model_error": res.error}
        if res.obj is None:
            return {"handled": False, "reason": "model_" + (res.error or "none"), **info}
        match, why = fastlane.check_model_output(res.obj, text, ctx)
        if match is None:
            return {"handled": False, "reason": why, **info}
    if fastlane.refers_back(match.check_text if match.check_text is not None else text):
        return {"handled": False, "reason": "refers_back", **info}
    try:
        reply = fastlane.execute(match, ctx, dry_run=True)
    except fastlane.PassUp as e:
        return {"handled": False, "reason": e.reason, **info}
    return {"handled": True, "tier": match.tier, "action": match.action, "fields": match.fields,
            "would_reply": reply.text, "writes": fastlane.ACTIONS[match.action].writes, **info}


def _read_texts(path: Path) -> list[str]:
    texts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            texts.append(line)
            continue
        if isinstance(obj, dict):
            if obj.get("type") == "turn_start" or ("text" in obj and "type" not in obj):
                texts.append(str(obj.get("text") or ""))
        elif isinstance(obj, str):
            texts.append(obj)
    return texts


def _router():
    import fastlane_model
    return fastlane_model.HaikuRouter(timeout=25.0)   # cold start allowed here: this is a CLI, not the worker


def cmd_test(args) -> int:
    text = (args.text or "").strip()
    if not text:
        print(json.dumps({"error": "text is required"}), file=sys.stderr)
        return 2
    today = None
    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            print(json.dumps({"error": "today must be YYYY-MM-DD"}), file=sys.stderr)
            return 2
    ctx = fastlane.Context.load(args.db, today)

    async def go():
        router = None if args.tier0_only else _router()
        try:
            return await classify(text, ctx, router)
        finally:
            if router:
                await router.close()

    res = asyncio.run(go())
    _out(res)
    return 0 if res["handled"] else 1


def cmd_eval(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(json.dumps({"error": f"no such file: {path}"}), file=sys.stderr)
        return 2
    texts = _read_texts(path)
    if not texts:
        print(json.dumps({"error": "no messages found in the file"}), file=sys.stderr)
        return 2
    ctx = fastlane.Context.load(args.db)
    by_action: dict[str, int] = {}
    taken: list[dict] = []
    undecided: list[int] = []
    for i, t in enumerate(texts):
        res = asyncio.run(classify(t, ctx, None))
        if res["handled"]:
            by_action[res["action"]] = by_action.get(res["action"], 0) + 1
            taken.append({"index": i, "tier": 0, "action": res["action"], **({"text": t} if args.show else {})})
        else:
            undecided.append(i)
    report: dict = {"messages": len(texts), "tier0_taken": len(taken), "tier0_coverage_pct": round(100 * len(taken) / len(texts), 1),
                    "tier0_by_action": by_action, "taken": taken}
    if args.tier1_sample and not args.tier0_only:
        pool = [i for i in undecided if _static_reason(texts[i]) is None and fastlane.worth_model(texts[i], ctx)]
        random.Random(7).shuffle(pool)
        sample = pool[: args.tier1_sample]

        async def run_t1():
            router = _router()
            await router.prewarm()
            out = []
            try:
                for i in sample:
                    r = await classify(texts[i], ctx, router)
                    out.append((i, r))
            finally:
                await router.close()
            return out

        results = asyncio.run(run_t1())
        lat = [r["model_latency_s"] for _, r in results if "model_latency_s" in r]
        t1 = [{"index": i, "tier": 1, "action": r["action"], "fields": r["fields"] if args.show else "(hidden)",
               **({"text": texts[i]} if args.show else {})} for i, r in results if r["handled"]]
        report["tier1"] = {"pool": len(pool), "sampled": len(sample), "taken": len(t1), "taken_items": t1,
                           "latency_s": {"median": round(statistics.median(lat), 2), "max": round(max(lat), 2),
                                         "n": len(lat)} if lat else None,
                           "reasons": _count(r.get("reason") for _, r in results if not r["handled"])}
    _out(report)
    return 0


def _count(items) -> dict:
    out: dict = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("text")
    t.add_argument("--tier0-only", action="store_true")
    t.add_argument("--db")
    t.add_argument("--today")
    t.set_defaults(fn=cmd_test)
    e = sub.add_parser("eval")
    e.add_argument("--file", required=True)
    e.add_argument("--tier1-sample", type=int, default=0)
    e.add_argument("--tier0-only", action="store_true")
    e.add_argument("--show", action="store_true")
    e.add_argument("--db")
    e.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
