"""Command line interface: `python -m orchestrator.cli --help`."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .engine import TERMINAL, Engine, EngineError
from .metrics import global_metrics, run_metrics
from .scenarios import SCENARIOS, start_scenario


def _engine(args) -> Engine:
    return Engine(args.data_dir)


def _print_run(engine: Engine, run_id: str) -> None:
    run = engine.get_run(run_id)
    print(f"\n{run['title']}  [{run['id']}]  status={run['status']}")
    if run["halt_reason"]:
        print(f"  halted: {run['halt_reason']}")
    for nid, n in run["nodes"].items():
        extra = []
        if n["retries"]:
            extra.append(f"retries={n['retries']}")
        if n["rework"]:
            extra.append(f"reworks={n['rework']}")
        if n["fallback_active"]:
            extra.append("fallback")
        print(f"  {nid:<18} {n['status']:<18} {n['summary'][:70]} {' '.join(extra)}")
    v = engine.audit.verify(run_id)
    print(f"  audit: {v.events} events, chain {'OK' if v.ok else 'BROKEN at ' + str(v.broken_at)}")
    if run["status"] in ("WAITING_INPUT", "WAITING_APPROVAL"):
        print("  waiting on a human. Use `approve`, or run the dashboard.")


def cmd_scenarios(args) -> int:
    for s in SCENARIOS.values():
        print(f"{s.id:<12} {s.title}\n             {s.description}\n")
    return 0


def cmd_run(args) -> int:
    engine = _engine(args)
    try:
        run_id = start_scenario(engine, args.scenario, clean=args.clean, requirement=args.requirement,
                                autopilot=args.autopilot)
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            st = engine.get_run(run_id)["status"]
            if st in TERMINAL or (not args.autopilot and st.startswith("WAITING")):
                break
            time.sleep(0.3)
        else:
            print("timed out", file=sys.stderr)
        time.sleep(0.2)
        _print_run(engine, run_id)
        if args.metrics:
            print(json.dumps(run_metrics(engine.audit.events(run_id), engine.get_run(run_id)), indent=2))
        return 0 if engine.get_run(run_id)["status"] == "SUCCEEDED" else 1
    finally:
        engine.close()


def cmd_runs(args) -> int:
    engine = _engine(args)
    try:
        for r in engine.list_runs():
            print(f"{r['id']}  {r['status']:<17} {r['title']}")
        return 0
    finally:
        engine.close()


def cmd_show(args) -> int:
    engine = _engine(args)
    try:
        _print_run(engine, args.run_id)
        if args.events:
            for e in engine.audit.events(args.run_id):
                print(f"{e['seq']:>4} {e['actor']:<28} {e['type']:<20} {e['node_id'] or '':<16} "
                      f"{json.dumps(e['payload'])[:110]}")
        return 0
    finally:
        engine.close()


def cmd_approve(args) -> int:
    engine = _engine(args)
    try:
        engine.decide_approval(args.run_id, args.node, args.decision, reviewer=args.by, comment=args.comment or "",
                               amendment=args.amendment)
        engine.advance(args.run_id)
        _print_run(engine, args.run_id)
        return 0
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.close()


def cmd_metrics(args) -> int:
    engine = _engine(args)
    try:
        print(json.dumps(global_metrics(engine.db, engine.audit), indent=2))
        return 0
    finally:
        engine.close()


def cmd_verify(args) -> int:
    engine = _engine(args)
    try:
        ids = [args.run_id] if args.run_id else [r["id"] for r in engine.list_runs()]
        bad = 0
        for rid in ids:
            v = engine.audit.verify(rid)
            print(f"{rid}  {'OK' if v.ok else 'BROKEN: ' + v.reason + ' at event ' + str(v.broken_at)}  ({v.events} events)")
            bad += not v.ok
        return 1 if bad else 0
    finally:
        engine.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator", description="Agentic software engineering orchestrator")
    p.add_argument("--data-dir", default=os.getenv("ORCH_DATA_DIR", "data/orchestrator"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scenarios", help="list scenarios").set_defaults(fn=cmd_scenarios)
    r = sub.add_parser("run", help="run a scenario")
    r.add_argument("scenario", choices=list(SCENARIOS))
    r.add_argument("--autopilot", action="store_true", help="a scripted reviewer makes the human decisions")
    r.add_argument("--clean", action="store_true", help="no injected faults")
    r.add_argument("--requirement")
    r.add_argument("--metrics", action="store_true")
    r.add_argument("--timeout", type=float, default=300)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("runs", help="list runs").set_defaults(fn=cmd_runs)
    s = sub.add_parser("show", help="show a run")
    s.add_argument("run_id")
    s.add_argument("--events", action="store_true")
    s.set_defaults(fn=cmd_show)
    a = sub.add_parser("approve", help="decide a pending approval")
    a.add_argument("run_id")
    a.add_argument("node")
    a.add_argument("decision", choices=["approve", "reject", "request_changes"])
    a.add_argument("--by", default="cli")
    a.add_argument("--comment")
    a.add_argument("--amendment")
    a.set_defaults(fn=cmd_approve)
    sub.add_parser("metrics", help="reliability metrics across all runs").set_defaults(fn=cmd_metrics)
    v = sub.add_parser("verify-audit", help="verify audit hash chains")
    v.add_argument("run_id", nargs="?")
    v.set_defaults(fn=cmd_verify)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
