"""Reliability metrics, derived only from the audit log and run state.

Nothing here is tracked separately: if a number is on the dashboard, the events that
produce it are in the audit trail and can be checked by hand.

Definitions
- success rate:       runs that SUCCEEDED / runs that reached a terminal state.
- retry frequency:    node_retry events per stage execution started.
- rework / rollback / fallback frequency: same denominator.
- MTTR:               mean seconds from a stage's node_failed to that stage's next node_succeeded
                      (failures that were never recovered are excluded and counted separately).
- end-to-end latency: run created -> run finished (wall clock, includes human waiting time).
- active latency:     end-to-end minus time spent waiting on a human (approvals, clarification).
- human wait:         approval_requested -> approval_decision, waiting_input -> input_received.
"""
from __future__ import annotations

from statistics import mean, median

from .audit import AuditLog
from .db import Database

TERMINAL = ("SUCCEEDED", "HALTED")


def _ratio(n: float, d: float) -> float | None:
    return round(n / d, 4) if d else None


def _round(x: float | None, places: int = 3) -> float | None:
    return None if x is None else round(x, places)


def run_metrics(events: list[dict], run: dict | None = None) -> dict:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1

    started = counts.get("node_started", 0)
    open_failure: dict[str, float] = {}
    recoveries: list[float] = []
    unrecovered = 0
    running_since: dict[str, float] = {}
    stage_seconds: dict[str, float] = {}
    wait_open: dict[str, float] = {}
    human_wait = 0.0

    for e in events:
        t, nid, ts = e["type"], e["node_id"], e["ts"]
        if t == "node_started" and nid:
            running_since[nid] = ts
        elif t in ("node_succeeded", "node_failed") and nid and nid in running_since:
            stage_seconds[nid] = stage_seconds.get(nid, 0.0) + (ts - running_since.pop(nid))
        if t == "node_failed" and nid:
            open_failure.setdefault(nid, ts)
        elif t == "node_succeeded" and nid and nid in open_failure:
            recoveries.append(ts - open_failure.pop(nid))
        if t == "approval_requested" and nid:
            wait_open[f"a:{nid}"] = ts
        elif t == "approval_decision" and nid and f"a:{nid}" in wait_open:
            human_wait += ts - wait_open.pop(f"a:{nid}")
        elif t == "waiting_input":
            wait_open.setdefault("input", ts)
        elif t == "input_received" and "input" in wait_open:
            human_wait += ts - wait_open.pop("input")

    unrecovered = len(open_failure)
    first = events[0]["ts"] if events else None
    last = events[-1]["ts"] if events else None
    finished = bool(run and run.get("finished_at"))
    end = run["finished_at"] if finished else last
    e2e = (end - first) if first is not None and end is not None else None
    active = None if e2e is None else max(0.0, e2e - human_wait)

    return {
        "status": run["status"] if run else None,
        "stage_executions": started,
        "stage_failures": counts.get("node_failed", 0),
        "retries": counts.get("node_retry", 0),
        "fallbacks": counts.get("node_fallback", 0),
        "reworks": counts.get("rework", 0),
        "rollbacks": counts.get("rollback", 0),
        "replans": counts.get("graph_replanned", 0),
        "policy_violations": counts.get("policy_violation", 0),
        "discarded_results": counts.get("result_discarded", 0),
        "early_cutoffs": counts.get("output_unchanged", 0),
        "approvals_requested": counts.get("approval_requested", 0),
        "approvals_decided": counts.get("approval_decision", 0),
        "retry_frequency": _ratio(counts.get("node_retry", 0), started),
        "rework_frequency": _ratio(counts.get("rework", 0), started),
        "rollback_frequency": _ratio(counts.get("rollback", 0), started),
        "mttr_seconds": _round(mean(recoveries)) if recoveries else None,
        "recovered_failures": len(recoveries),
        "unrecovered_failures": unrecovered,
        "end_to_end_seconds": _round(e2e),
        "active_seconds": _round(active),
        "human_wait_seconds": _round(human_wait),
        "stage_seconds": {k: _round(v) for k, v in sorted(stage_seconds.items())},
        "events": len(events),
    }


def global_metrics(db: Database, audit: AuditLog) -> dict:
    rows = db.query("SELECT id, scenario, status, created_at, finished_at FROM runs ORDER BY created_at")
    per_run: list[dict] = []
    for r in rows:
        m = run_metrics(audit.events(r["id"]), dict(r))
        m.update(run_id=r["id"], scenario=r["scenario"])
        per_run.append(m)

    terminal = [m for m in per_run if m["status"] in TERMINAL]
    succeeded = [m for m in terminal if m["status"] == "SUCCEEDED"]
    execs = sum(m["stage_executions"] for m in per_run)
    mttrs = [m["mttr_seconds"] for m in per_run if m["mttr_seconds"] is not None]
    e2es = [m["end_to_end_seconds"] for m in succeeded if m["end_to_end_seconds"] is not None]
    actives = [m["active_seconds"] for m in succeeded if m["active_seconds"] is not None]

    def total(key: str) -> int:
        return sum(m[key] for m in per_run)

    by_scenario: dict[str, dict] = {}
    for m in per_run:
        s = by_scenario.setdefault(m["scenario"], {"runs": 0, "succeeded": 0, "halted": 0})
        s["runs"] += 1
        s["succeeded"] += m["status"] == "SUCCEEDED"
        s["halted"] += m["status"] == "HALTED"

    return {
        "runs": len(per_run),
        "terminal_runs": len(terminal),
        "succeeded": len(succeeded),
        "halted": len(terminal) - len(succeeded),
        "in_progress": len(per_run) - len(terminal),
        "success_rate": _ratio(len(succeeded), len(terminal)),
        "stage_executions": execs,
        "retries": total("retries"),
        "fallbacks": total("fallbacks"),
        "reworks": total("reworks"),
        "rollbacks": total("rollbacks"),
        "replans": total("replans"),
        "policy_violations": total("policy_violations"),
        "retry_frequency": _ratio(total("retries"), execs),
        "rework_frequency": _ratio(total("reworks"), execs),
        "rollback_frequency": _ratio(total("rollbacks"), execs),
        "mttr_seconds": _round(mean(mttrs)) if mttrs else None,
        "e2e_seconds_mean": _round(mean(e2es)) if e2es else None,
        "e2e_seconds_median": _round(median(e2es)) if e2es else None,
        "active_seconds_mean": _round(mean(actives)) if actives else None,
        "by_scenario": by_scenario,
        "note": "Computed from the audit log. Scenario runs include deliberately injected faults, so retry and "
                "rollback rates here describe the demos, not production behaviour.",
    }
