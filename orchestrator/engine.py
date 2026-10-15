"""The orchestration engine.

A run is a stateful walk over an explicit dependency graph. The engine owns every state
transition; agents only produce outputs. Key behaviours:

* Dataflow scheduling: any stage whose ancestors have all succeeded is launched, in parallel,
  and results are joined as they complete.
* Gates: entry gates before a stage starts, exit gates before its output is accepted.
* Content-addressed outputs: each accepted output has a digest. A consumer records the digests
  it was built from; when an upstream digest changes the consumer goes STALE and re-runs, and when
  a re-run produces an identical digest nothing downstream is touched (early cutoff).
* Failure ladder: bounded retry -> fallback agent -> rework of the responsible upstream stage with
  the failure as feedback -> rollback and safe-stop.
* Human control: approvals (approve / reject / request changes), clarification answers, amendments
  and a stop switch, all recorded in the hash-chained audit log.
* Every state transition is persisted, so a run survives a restart (`recover`).
"""
from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from . import gates, pipeline
from .agents import default_registry
from .agents.base import AgentContext, AgentOutput, AgentRefusal, AgentRegistry, TransientAgentError
from .audit import AuditLog
from .catalog import REPO_ROOT
from .db import Database
from .graph import Graph, NodeSpec
from .policy import Policy, PolicyViolation
from .util import canonical_json, digest, new_id, now
from .workspace import Workspace

TERMINAL = {"SUCCEEDED", "HALTED", "FAILED"}
SETTLED_NODE = {"SUCCEEDED"}


class EngineError(Exception):
    """A request that is not valid in the run's current state."""


def _new_node() -> dict:
    return {
        "status": "PENDING", "attempt": 0, "retries": 0, "rework": 0, "fallback_active": False,
        "agent": None, "output": None, "output_digest": None, "input_digests": None,
        "launch_inputs": None, "launch_artifacts": None, "artifact_id": None, "approval": None,
        "checkpoint": None, "failure": None, "feedback": None, "token": None,
        "started_at": None, "finished_at": None, "last_failure_at": None, "not_before": 0.0,
        "summary": "", "evidence": {}, "commit": None, "gates": [], "last_exec_s": None, "last_seen": None,
    }


class Engine:
    def __init__(
        self,
        data_dir: str | Path,
        policy: Policy | None = None,
        registry: AgentRegistry | None = None,
        repo_root: Path = REPO_ROOT,
        max_workers: int = 4,
        backoff: Callable[[int], float] = lambda attempt: 0.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or Policy.load(repo_root / "policy.json")
        self.registry = registry or default_registry()
        self.repo_root = Path(repo_root)
        self.backoff = backoff
        self.db = Database(self.data_dir / "orchestrator.db")
        self.audit = AuditLog(self.db)
        self.raise_internal_errors = False
        self._pool = ThreadPoolExecutor(max_workers, thread_name_prefix="agent")
        self._drivers = ThreadPoolExecutor(8, thread_name_prefix="driver")
        self._runs: dict[str, dict] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._inflight: dict[str, dict[str, tuple[Future, str]]] = {}
        self._driving: set[str] = set()
        self._dirty: set[str] = set()
        self._mx = threading.Lock()

    # ====================================================================================
    # plumbing
    # ====================================================================================
    def close(self) -> None:
        self._drivers.shutdown(wait=True)
        self._pool.shutdown(wait=True)
        self.db.close()

    def _lock(self, run_id: str) -> threading.RLock:
        with self._mx:
            return self._locks.setdefault(run_id, threading.RLock())

    def run_dir(self, run_id: str) -> Path:
        return self.data_dir / "runs" / run_id

    def workspace(self, run_id: str) -> Workspace:
        with self._mx:
            ws = self._workspaces.get(run_id)
            if ws is None:
                d = self.run_dir(run_id)
                ws = Workspace(d / "workspace", d / "checkpoints", self.policy)
                self._workspaces[run_id] = ws
            return ws

    def _run(self, run_id: str) -> dict:
        with self._mx:
            run = self._runs.get(run_id)
        if run is None:
            row = self.db.one("SELECT state FROM runs WHERE id = ?", (run_id,))
            if row is None:
                raise EngineError(f"unknown run {run_id}")
            run = json.loads(row["state"])
            with self._mx:
                run = self._runs.setdefault(run_id, run)
        return run

    def _save(self, run: dict) -> None:
        run["updated_at"] = now()
        self.db.execute(
            "INSERT INTO runs (id, scenario, title, status, created_at, updated_at, finished_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "updated_at=excluded.updated_at, finished_at=excluded.finished_at, state=excluded.state",
            (run["id"], run["scenario"], run["title"], run["status"], run["created_at"],
             run["updated_at"], run["finished_at"], canonical_json(run)),
        )

    def _emit(self, run_id: str, type: str, actor: str = "system", node_id: str | None = None,
              payload: dict | None = None) -> dict:
        return self.audit.append(run_id, type, actor, node_id, payload)

    @staticmethod
    def _graph(run: dict) -> Graph:
        return Graph.from_dict(run["graph"])

    # ====================================================================================
    # public: create / read
    # ====================================================================================
    def create_run(
        self,
        scenario: str,
        requirement: str,
        mode: str,
        *,
        title: str | None = None,
        seed_files: dict[str, str] | None = None,
        faults: dict | None = None,
        config: dict | None = None,
    ) -> str:
        run_id = new_id("run")
        graph = pipeline.build_graph(self.policy)
        t = now()
        run = {
            "id": run_id, "scenario": scenario, "title": title or scenario, "requirement": requirement,
            "mode": mode, "config": config or {}, "faults": faults or {}, "status": "RUNNING",
            "halt_reason": None, "created_at": t, "updated_at": t, "finished_at": None,
            "active_seconds": 0.0, "graph": graph.to_dict(),
            "nodes": {nid: _new_node() for nid in graph.nodes},
            "amendments": [], "answers": {}, "answer_sources": {}, "decisions": [], "artifacts": [],
            "approvals_log": [], "stop_requested": None, "halted_running": [],
        }
        ws = self.workspace(run_id)
        with self._lock(run_id):
            with self._mx:
                self._runs[run_id] = run
            self._emit(run_id, "run_created", "system", None, {
                "scenario": scenario, "mode": mode, "requirement": requirement, "faults": run["faults"],
                "policy_digest": digest(self.policy.to_dict())[:16], "stages": list(graph.nodes),
            })
            if seed_files:
                ws.seed(seed_files)
                self._emit(run_id, "workspace_seeded", "system", None, {"files": len(seed_files)})
            # Immutable copy of the starting codebase: agents analyse this, never their own earlier output.
            run["baseline_checkpoint"] = ws.snapshot(["**"], "baseline")
            self._save(run)
        return run_id

    def get_run(self, run_id: str) -> dict:
        with self._lock(run_id):
            return copy.deepcopy(self._run(run_id))

    def list_runs(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, scenario, title, status, created_at, updated_at, finished_at FROM runs ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    def count_active(self) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM runs WHERE status = 'RUNNING'")
        return int(row["n"])

    # ====================================================================================
    # driving a run
    # ====================================================================================
    def advance(self, run_id: str) -> str:
        """Drive the run until it finishes or needs a human. Safe to call from several threads."""
        with self._mx:
            if run_id in self._driving:
                self._dirty.add(run_id)  # the active driver will loop again before it exits
                return self._runs.get(run_id, {}).get("status", "RUNNING")
            self._driving.add(run_id)
        try:
            while True:
                try:
                    self._drive(run_id)
                except Exception as exc:
                    self._internal_error(run_id, exc)
                    if self.raise_internal_errors:
                        raise
                with self._mx:
                    if run_id in self._dirty:
                        self._dirty.discard(run_id)
                        continue
                    self._driving.discard(run_id)
                    break
        except BaseException:
            with self._mx:
                self._driving.discard(run_id)
            raise
        return self.get_run(run_id)["status"]

    def advance_async(self, run_id: str) -> None:
        self._drivers.submit(self._safe_advance, run_id)

    def submit(self, fn: Callable, *args) -> Future:
        """Run a callable on the driver pool (used for scripted reviewers)."""
        return self._drivers.submit(fn, *args)

    def _safe_advance(self, run_id: str) -> None:
        try:
            self.advance(run_id)
        except Exception:
            pass  # already recorded as an internal_error event

    def _internal_error(self, run_id: str, exc: Exception) -> None:
        with self._lock(run_id):
            run = self._run(run_id)
            self._emit(run_id, "engine_error", "system", None, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            if run["status"] not in TERMINAL:
                self._halt(run, f"internal_error: {type(exc).__name__}")

    def _drive(self, run_id: str) -> None:
        lock = self._lock(run_id)
        run = self._run(run_id)
        started = time.monotonic()
        try:
            while True:
                with lock:
                    if run["status"] in TERMINAL:
                        return
                    if run["status"] in ("WAITING_INPUT", "WAITING_APPROVAL"):
                        run["status"] = "RUNNING"
                    if run["stop_requested"]:
                        s = run["stop_requested"]
                        self._halt(run, f"stopped by {s['by']}: {s['reason']}", actor=f"human:{s['by']}")
                        return
                    if run["active_seconds"] + (time.monotonic() - started) > self.policy.max_run_seconds:
                        self._halt(run, "deadline_exceeded")
                        return
                    graph = self._graph(run)
                    self._reconcile(run, graph)
                    self._launch_ready(run, graph)
                    if run["status"] in TERMINAL:
                        return
                    inflight = self._inflight.get(run_id, {})
                    if not inflight:
                        outcome = self._settle(run, graph)
                        if isinstance(outcome, tuple):
                            delay = outcome[1]
                        elif outcome in ("done", "blocked", "stuck"):
                            return
                        else:
                            delay = 0.0
                        futures = None
                    else:
                        futures = {f: nid for nid, (f, _tok) in inflight.items()}
                        delay = 0.0
                    self._save(run)
                if futures is None:
                    if delay:
                        time.sleep(min(delay, 0.25))
                    continue
                done, _ = wait(list(futures), timeout=0.25, return_when="FIRST_COMPLETED")
                for f in done:
                    with lock:
                        self._complete(run, futures[f])
                        self._save(run)
        finally:
            with lock:
                run["active_seconds"] += time.monotonic() - started
                self._save(run)
            if run["status"] in TERMINAL:
                self._finish_halt(run_id)

    # ---- scheduling ------------------------------------------------------------------------
    def _ancestors_ok(self, run: dict, graph: Graph, nid: str) -> bool:
        return all(run["nodes"][a]["status"] in SETTLED_NODE for a in graph.ancestors(nid))

    def _launch_ready(self, run: dict, graph: Graph) -> None:
        inflight = self._inflight.setdefault(run["id"], {})
        t = now()
        for nid in graph.topo_order():
            n = run["nodes"][nid]
            if n["status"] not in ("PENDING", "STALE") or nid in inflight or n["not_before"] > t:
                continue
            if not self._ancestors_ok(run, graph, nid):
                continue
            spec = graph.nodes[nid]
            for g in spec.entry_gates:
                res = gates.evaluate(g, gates.GateContext(run, spec, graph, None, self.workspace(run["id"]), self.policy))
                self._emit(run["id"], "gate_evaluated", "gate", nid,
                           {"phase": "entry", "gate": g, "passed": res.passed, "detail": res.detail})
                if not res.passed:
                    self._halt(run, f"entry_gate_failed:{nid}:{g}: {res.detail}")
                    return
            self._start_node(run, graph, spec)

    def _pick_agent(self, spec: NodeSpec, n: dict) -> str:
        if spec.fallback_agent and (n["fallback_active"] or n["rework"] >= 1):
            return spec.fallback_agent
        return spec.agent

    def _facts(self, run: dict) -> dict:
        c = self.audit.counts(run["id"])
        return {
            "retries": c.get("node_retry", 0), "reworks": c.get("rework", 0),
            "rollbacks": c.get("rollback", 0), "replans": c.get("graph_replanned", 0),
            "policy_violations": c.get("policy_violation", 0),
            "approvals": [
                {"node": nid, "status": n["approval"]["status"], "by": n["approval"].get("by")}
                for nid, n in run["nodes"].items() if n["approval"]
            ],
            "amendments": [a["text"] for a in run["amendments"]],
            "answers": dict(run["answers"]),
        }

    def _make_ctx(self, run: dict, graph: Graph, spec: NodeSpec, n: dict, agent_key: str) -> AgentContext:
        ws = self.workspace(run["id"])
        agent = self.registry.get(agent_key)
        run_id = run["id"]
        scoped = ws.scoped(
            spec.id, spec.write_scope, f"agent:{agent.name}",
            emit=lambda t, a, nid, p, rid=run_id: self._emit(rid, t, a, nid, p),
        )
        inputs = {
            a: copy.deepcopy(run["nodes"][a]["output"])
            for a in graph.ancestors(spec.id) if run["nodes"][a]["output"] is not None
        }
        return AgentContext(
            run_id=run_id, node_id=spec.id, agent_key=agent_key, attempt=n["attempt"],
            is_fallback=agent_key != spec.agent, rework=n["rework"],
            requirement=run["requirement"], amendments=[a["text"] for a in run["amendments"]],
            answers=dict(run["answers"]), answer_sources=dict(run["answer_sources"]), mode=run["mode"],
            inputs=inputs, feedback=copy.deepcopy(n["feedback"]), run_facts=self._facts(run),
            workspace=scoped, full_workspace=ws,
            baseline_dir=ws.checkpoint_files_dir(run["baseline_checkpoint"]),
            checkpoints={nid: x["checkpoint"] for nid, x in run["nodes"].items() if x["checkpoint"]},
            faults=run["faults"], policy=self.policy, repo_root=self.repo_root,
            run_dir=self.run_dir(run_id), config=run["config"],
        )

    def _start_node(self, run: dict, graph: Graph, spec: NodeSpec) -> None:
        n = run["nodes"][spec.id]
        ws = self.workspace(run["id"])
        n["attempt"] += 1
        agent_key = self._pick_agent(spec, n)
        if spec.write_scope:
            if n["checkpoint"] is None:
                n["checkpoint"] = ws.snapshot(spec.write_scope, spec.id)
            else:
                ws.restore(n["checkpoint"])
        token = new_id("tok")
        n.update(
            status="RUNNING", agent=agent_key, token=token, started_at=now(), finished_at=None, failure=None,
            gates=[], launch_inputs={d: run["nodes"][d]["output_digest"] for d in spec.deps},
            launch_artifacts={d: run["nodes"][d]["artifact_id"] for d in spec.deps},
        )
        ctx = self._make_ctx(run, graph, spec, n, agent_key)
        agent = self.registry.get(agent_key)
        self._emit(run["id"], "node_started", f"agent:{agent.name}", spec.id, {
            "attempt": n["attempt"], "agent": agent_key, "fallback": ctx.is_fallback, "rework": n["rework"],
            "inputs": n["launch_artifacts"], "has_feedback": bool(n["feedback"]),
        })
        fut = self._pool.submit(self._invoke, agent, ctx)
        self._inflight.setdefault(run["id"], {})[spec.id] = (fut, token)

    @staticmethod
    def _invoke(agent, ctx: AgentContext) -> dict:
        t0 = time.monotonic()
        try:
            out = agent.run(ctx)
            return {"status": "ok", "output": out, "duration": time.monotonic() - t0}
        except PolicyViolation as exc:
            return {"status": "policy", "violations": [v.to_dict() for v in exc.violations], "detail": str(exc)}
        except TransientAgentError as exc:
            return {"status": "transient", "detail": str(exc)}
        except AgentRefusal as exc:
            return {"status": "refused", "detail": str(exc)}
        except Exception as exc:  # unexpected agent crash
            return {"status": "agent_error", "detail": f"{type(exc).__name__}: {exc}"[:500]}

    # ---- completion --------------------------------------------------------------------------
    def _complete(self, run: dict, nid: str) -> None:
        fut, tok = self._inflight[run["id"]].pop(nid)
        res = fut.result()
        n = run["nodes"].get(nid)
        if n is None or n["token"] != tok or n["status"] != "RUNNING":
            self._emit(run["id"], "result_discarded", "system", nid,
                       {"reason": "the stage was invalidated while it was running"})
            return
        graph = self._graph(run)
        spec = graph.nodes[nid]
        st = res["status"]
        if st == "ok":
            n["last_exec_s"] = round(res["duration"], 3)
            self._on_output(run, graph, spec, n, res["output"])
        elif st == "refused":
            self._emit(run["id"], "node_failed", f"agent:{n['agent']}", nid,
                       {"kind": "refused", "detail": res["detail"], "attempt": n["attempt"]})
            n["status"], n["failure"] = "FAILED", {"kind": "refused", "gate": None, "detail": res["detail"]}
            self._rollback(run, spec, "agent refused")
            self._halt(run, f"escalated_to_human:{nid}: {res['detail']}")
        elif st == "policy":
            self._handle_failure(run, graph, spec, n, {
                "kind": "policy", "gate": None, "detail": res["detail"][:600], "violations": res["violations"],
            })
        else:
            self._handle_failure(run, graph, spec, n, {"kind": "transient" if st == "transient" else "agent_error",
                                                       "gate": None, "detail": res["detail"][:600]})

    def _on_output(self, run: dict, graph: Graph, spec: NodeSpec, n: dict, out: AgentOutput) -> None:
        n["summary"], n["evidence"] = out.summary, out.evidence
        seen = digest(out.data)
        if n["feedback"] and n["last_seen"] == seen:
            # Reworked with feedback and produced exactly what already failed: more attempts cannot help.
            n["last_seen"] = seen
            n["status"] = "FAILED"
            cause = n["feedback"]["failure"]
            n["failure"] = {"kind": "rework_futile", "gate": None,
                            "detail": "the agent produced an identical artifact after receiving failure feedback. "
                                      f"Original failure ({n['feedback']['from']}"
                                      f"{': ' + cause['gate'] if cause.get('gate') else ''}): {cause['detail'][:300]}"}
            self._emit(run["id"], "rework_futile", f"agent:{n['agent']}", spec.id,
                       {"digest": seen[:12], "feedback_from": n["feedback"]["from"],
                        "feedback_gate": n["feedback"]["failure"].get("gate")})
            self._rollback(run, spec, "rework produced identical output")
            self._halt(run, f"rework_futile:{spec.id}: identical output after feedback from '{n['feedback']['from']}' "
                            f"({n['feedback']['failure']['detail'][:200]}); escalating to a human")
            return
        n["last_seen"] = seen
        if spec.id == "design":
            changes = pipeline.apply_adjustments(graph, out.data, self.policy)
            if changes["added"] or changes["removed"]:
                for nid in changes["added"]:
                    run["nodes"][nid] = _new_node()
                for nid in changes["removed"]:
                    run["nodes"].pop(nid, None)
                run["graph"] = graph.to_dict()
                self._emit(run["id"], "graph_replanned", "planner", "design", {
                    **changes, "reason": f"design impact tags: {out.data['impact_tags']}",
                    "edges": [f"{a}->{b}" for a, b in graph.edges()],
                })
        ws = self.workspace(run["id"])
        results = []
        for g in spec.exit_gates:
            r = gates.evaluate(g, gates.GateContext(run, spec, graph, out.data, ws, self.policy))
            results.append({"gate": g, "passed": r.passed, "detail": r.detail})
            self._emit(run["id"], "gate_evaluated", "gate", spec.id,
                       {"phase": "exit", "gate": g, "passed": r.passed, "detail": r.detail})
            if not r.passed:
                n["gates"] = results
                if r.kind == "needs_input":
                    self._accept(run, graph, spec, n, out)
                    n["status"] = "WAITING_INPUT"
                    self._emit(run["id"], "waiting_input", "system", spec.id, {
                        "questions": [q["id"] for q in out.data["open_questions"] if q["blocking"] and not q["answered"]],
                    })
                    return
                self._handle_failure(run, graph, spec, n, {"kind": "gate", "gate": g, "detail": r.detail[:600]})
                return
        n["gates"] = results
        self._accept(run, graph, spec, n, out)
        reasons = self._approval_reasons(spec, out.data)
        if reasons:
            ap = n["approval"]
            if not (ap and ap["status"] == "granted" and ap["digest"] == n["output_digest"]):
                n["approval"] = {
                    "status": "pending", "digest": n["output_digest"], "reasons": reasons,
                    "requested_at": now(), "review": out.data.get("review") or {"checklist": out.data.get("checklist")},
                }
                n["status"] = "AWAITING_APPROVAL"
                self._emit(run["id"], "approval_requested", "system", spec.id,
                           {"reasons": reasons, "digest": n["output_digest"][:12]})
                return
        self._finalize(run, graph, spec, n)

    def _approval_reasons(self, spec: NodeSpec, data: dict) -> list[str]:
        if spec.approval == "always":
            return ["high-impact action: release"]
        if spec.approval == "by_impact":
            return self.policy.requires_approval(data.get("impact_tags", []))
        return []

    def _accept(self, run: dict, graph: Graph, spec: NodeSpec, n: dict, out: AgentOutput) -> None:
        dg = digest(out.data)
        if n["output_digest"] == dg:
            self._emit(run["id"], "output_unchanged", "system", spec.id,
                       {"digest": dg[:12], "note": "re-run produced an identical artifact; downstream stages are not invalidated"})
        else:
            version = 1 + sum(1 for a in run["artifacts"] if a["node"] == spec.id)
            art_id = f"{spec.id}@{version}"
            run["artifacts"].append({
                "id": art_id, "node": spec.id, "version": version, "digest": dg,
                "inputs": dict(n["launch_artifacts"] or {}), "at": now(),
            })
            n["artifact_id"] = art_id
            for d in out.decisions:
                prev = next((x for x in reversed(run["decisions"])
                             if x["node"] == spec.id and x["key"] == d["key"] and not x["superseded_by"]), None)
                rec = {"id": f"D-{len(run['decisions']) + 1}", "node": spec.id, "attempt": n["attempt"],
                       "artifact": art_id, **d, "supersedes": prev["id"] if prev else None, "superseded_by": None}
                if prev:
                    prev["superseded_by"] = rec["id"]
                run["decisions"].append(rec)
            self._emit(run["id"], "artifact_created", f"agent:{n['agent']}", spec.id,
                       {"artifact": art_id, "digest": dg[:12], "summary": out.summary,
                        "derived_from": n["launch_artifacts"]})
        n["output"], n["output_digest"], n["input_digests"] = out.data, dg, dict(n["launch_inputs"] or {})
        self._reconcile(run, graph)

    def _finalize(self, run: dict, graph: Graph, spec: NodeSpec, n: dict) -> None:
        agent = self.registry.get(n["agent"])
        commit = getattr(agent, "commit", None)
        if commit is not None:
            try:
                ctx = self._make_ctx(run, graph, spec, n, n["agent"])
                n["commit"] = commit(ctx, n["output"])
                self._emit(run["id"], "action_committed", f"agent:{agent.name}", spec.id, n["commit"])
            except Exception as exc:
                self._handle_failure(run, graph, spec, n,
                                     {"kind": "commit", "gate": None, "detail": f"{type(exc).__name__}: {exc}"[:500]})
                return
        n["status"], n["finished_at"], n["failure"], n["feedback"] = "SUCCEEDED", now(), None, None
        self._emit(run["id"], "node_succeeded", f"agent:{agent.name}", spec.id, {
            "attempt": n["attempt"], "digest": (n["output_digest"] or "")[:12], "duration_s": n["last_exec_s"],
            "agent": n["agent"],
        })

    # ---- failure ladder ----------------------------------------------------------------------
    def _handle_failure(self, run: dict, graph: Graph, spec: NodeSpec, n: dict, failure: dict) -> None:
        kind = failure["kind"]
        n["status"], n["failure"], n["last_failure_at"] = "FAILED", failure, now()
        self._emit(run["id"], "node_failed", f"agent:{n['agent']}", spec.id, {
            "kind": kind, "gate": failure.get("gate"), "detail": failure["detail"],
            "attempt": n["attempt"], "agent": n["agent"],
        })
        if kind == "policy":
            used = self.audit.counts(run["id"]).get("policy_violation", 0)
            if used >= self.policy.max_policy_violations:
                self._rollback(run, spec, "policy violation budget exhausted")
                self._halt(run, f"policy_violation_budget_exceeded ({used} violations)")
                return
        if kind in ("transient", "agent_error"):
            if n["retries"] < spec.max_retries:
                n["retries"] += 1
                delay = self.backoff(n["retries"])
                self._rollback(run, spec, f"retry {n['retries']} of {spec.max_retries}")
                n["status"], n["not_before"] = "PENDING", now() + delay
                self._emit(run["id"], "node_retry", "system", spec.id,
                           {"retry": n["retries"], "of": spec.max_retries, "backoff_s": delay, "reason": failure["detail"][:200]})
                return
            if spec.fallback_agent and not n["fallback_active"]:
                n["fallback_active"], n["retries"] = True, 0
                self._rollback(run, spec, "switching to fallback agent")
                n["status"] = "PENDING"
                self._emit(run["id"], "node_fallback", "system", spec.id, {"to": spec.fallback_agent})
                return
            target_id = spec.id
        else:
            target_id = (spec.rework_target or spec.id) if kind == "gate" else spec.id
            tspec = graph.nodes[target_id]
            if run["nodes"][target_id]["rework"] < tspec.max_rework:
                self._rework(run, graph, target_id, spec.id, failure)
                return
        self._rollback(run, graph.nodes[target_id], "bounded recovery exhausted")
        self._halt(run, f"retries_exhausted:{spec.id}:{failure.get('gate') or kind}")

    def _rework(self, run: dict, graph: Graph, target_id: str, cause_id: str, failure: dict) -> None:
        t = run["nodes"][target_id]
        tspec = graph.nodes[target_id]
        t["rework"] += 1
        t["feedback"] = {"from": cause_id, "failure": failure, "rework": t["rework"]}
        self._rollback(run, tspec, f"rework: '{cause_id}' failed {failure.get('gate') or failure['kind']}")
        self._emit(run["id"], "rework", "system", target_id, {
            "cause": cause_id, "gate": failure.get("gate"), "kind": failure["kind"],
            "detail": failure["detail"][:300], "rework": t["rework"], "of": tspec.max_rework,
            "next_agent": self._pick_agent(tspec, t),
        })
        for nid in [target_id, *sorted(graph.descendants(target_id))]:
            self._invalidate(run, nid, f"rework of {target_id}")

    def _rollback(self, run: dict, spec: NodeSpec, reason: str) -> None:
        n = run["nodes"].get(spec.id)
        if not spec.write_scope or not n or not n["checkpoint"]:
            return
        changes = self.workspace(run["id"]).restore(n["checkpoint"])
        self._emit(run["id"], "rollback", "system", spec.id, {
            "checkpoint": n["checkpoint"], "restored": changes["restored"], "deleted": changes["deleted"], "reason": reason,
        })

    def _invalidate(self, run: dict, nid: str, reason: str) -> None:
        n = run["nodes"].get(nid)
        if n is None or (n["status"] == "PENDING" and n["output_digest"] is None):
            return
        n["status"], n["token"], n["failure"] = "STALE", None, None
        self._emit(run["id"], "invalidated", "system", nid, {"reason": reason})

    def _reconcile(self, run: dict, graph: Graph) -> None:
        """Invalidate any stage built from inputs that have since changed."""
        for nid in graph.topo_order():
            n = run["nodes"][nid]
            if n["status"] not in ("SUCCEEDED", "AWAITING_APPROVAL", "WAITING_INPUT") or n["input_digests"] is None:
                continue
            expected = {d: run["nodes"][d]["output_digest"] for d in graph.nodes[nid].deps}
            if n["input_digests"] != expected:
                changed = sorted(d for d in expected if n["input_digests"].get(d) != expected[d])
                self._invalidate(run, nid, f"inputs changed: {', '.join(changed)}")

    def _settle(self, run: dict, graph: Graph):
        statuses = {nid: n["status"] for nid, n in run["nodes"].items()}
        if "WAITING_INPUT" in statuses.values():
            run["status"] = "WAITING_INPUT"
            return "blocked"
        if "AWAITING_APPROVAL" in statuses.values():
            run["status"] = "WAITING_APPROVAL"
            return "blocked"
        if all(s == "SUCCEEDED" for s in statuses.values()):
            run["status"], run["finished_at"] = "SUCCEEDED", now()
            self._emit(run["id"], "run_succeeded", "system", None, {"stages": len(statuses)})
            return "done"
        waits = [n["not_before"] - now() for n in run["nodes"].values()
                 if n["status"] in ("PENDING", "STALE") and n["not_before"] > now()]
        if waits:
            return ("wait", min(waits))
        self._halt(run, "deadlock: no stage can run and none is waiting for a human")
        return "stuck"

    # ---- halting -----------------------------------------------------------------------------
    def _halt(self, run: dict, reason: str, actor: str = "system") -> None:
        if run["status"] in TERMINAL:
            return
        running = [nid for nid, n in run["nodes"].items() if n["status"] == "RUNNING"]
        for nid in running:
            run["nodes"][nid]["status"], run["nodes"][nid]["token"] = "STALE", None
        run["halted_running"] = running
        run["status"], run["halt_reason"], run["finished_at"] = "HALTED", reason, now()
        self._emit(run["id"], "run_halted", actor, None, {
            "reason": reason, "stages": {nid: n["status"] for nid, n in run["nodes"].items()},
        })

    def _finish_halt(self, run_id: str) -> None:
        """After a halt: let in-flight agents finish, discard their results, roll back their writes."""
        with self._lock(run_id):
            futs = [f for f, _ in self._inflight.get(run_id, {}).values()]
        if futs:
            wait(futs, timeout=120)
        with self._lock(run_id):
            run = self._run(run_id)
            graph = self._graph(run)
            self._inflight.pop(run_id, None)
            for nid in run["halted_running"]:
                if nid in graph.nodes:
                    self._rollback(run, graph.nodes[nid], "safe-stop while the stage was running")
            if run["halted_running"]:
                run["halted_running"] = []
                self._save(run)

    # ====================================================================================
    # human actions
    # ====================================================================================
    def _require_open(self, run: dict) -> None:
        if run["status"] in TERMINAL:
            raise EngineError(f"run is {run['status']}")

    def provide_clarification(self, run_id: str, answers: dict, by: str = "human", use_defaults: bool = False) -> None:
        with self._lock(run_id):
            run = self._run(run_id)
            self._require_open(run)
            node = run["nodes"]["requirements"]
            if node["status"] != "WAITING_INPUT":
                raise EngineError("no clarification is currently requested")
            questions = {q["id"]: q for q in node["output"]["open_questions"]}
            unknown = sorted(set(answers) - set(questions))
            if unknown:
                raise EngineError(f"unknown question id(s): {unknown}")
            defaults_used = []
            for qid, q in questions.items():
                if qid in answers:
                    run["answers"][qid], run["answer_sources"][qid] = answers[qid], "human"
                elif use_defaults and not q["answered"] and q.get("default") is not None:
                    run["answers"][qid], run["answer_sources"][qid] = q["default"], "default"
                    defaults_used.append(qid)
            self._emit(run_id, "input_received", f"human:{by}", "requirements",
                       {"answers": {k: answers[k] for k in answers}, "defaults_used": defaults_used})
            self._invalidate(run, "requirements", "clarification received")
            run["status"] = "RUNNING"
            self._save(run)

    def amend(self, run_id: str, text: str, by: str = "human") -> None:
        text = (text or "").strip()
        if not text:
            raise EngineError("amendment text is empty")
        with self._lock(run_id):
            run = self._run(run_id)
            self._require_open(run)
            self._apply_amendment(run, text, by)
            self._save(run)

    def _apply_amendment(self, run: dict, text: str, by: str) -> None:
        run["amendments"].append({"text": text, "by": by, "at": now()})
        self._emit(run["id"], "requirement_amended", f"human:{by}", "requirements", {"text": text})
        req = run["nodes"]["requirements"]
        if req["status"] in ("PENDING",):
            return  # not yet started: it will pick the amendment up
        self._invalidate(run, "requirements", "requirement amended")
        run["status"] = "RUNNING"

    def decide_approval(self, run_id: str, node_id: str, decision: str, reviewer: str = "human",
                        comment: str = "", amendment: str | None = None) -> None:
        if decision not in ("approve", "reject", "request_changes"):
            raise EngineError("decision must be approve, reject or request_changes")
        with self._lock(run_id):
            run = self._run(run_id)
            self._require_open(run)
            n = run["nodes"].get(node_id)
            if n is None or n["status"] != "AWAITING_APPROVAL":
                raise EngineError(f"'{node_id}' is not awaiting approval")
            ap = n["approval"]
            if ap["digest"] != n["output_digest"]:
                raise EngineError("the artifact changed since approval was requested; review the new version")
            actor = f"human:{reviewer}"
            run["approvals_log"].append({"node": node_id, "decision": decision, "by": reviewer,
                                         "comment": comment, "digest": ap["digest"][:12], "at": now()})
            self._emit(run_id, "approval_decision", actor, node_id,
                       {"decision": decision, "comment": comment, "digest": ap["digest"][:12], "amendment": amendment})
            graph = self._graph(run)
            spec = graph.nodes[node_id]
            if decision == "approve":
                ap.update(status="granted", by=reviewer, at=now(), comment=comment)
                run["status"] = "RUNNING"
                self._finalize(run, graph, spec, n)
            elif decision == "reject":
                ap.update(status="rejected", by=reviewer, at=now(), comment=comment)
                n["status"], n["failure"] = "FAILED", {"kind": "rejected", "gate": None, "detail": comment or "rejected by reviewer"}
                self._halt(run, f"rejected_by:{reviewer}:{node_id}", actor=actor)
            else:
                text = (amendment or comment or "").strip()
                if not text:
                    raise EngineError("request_changes needs an amendment or a comment describing the change")
                ap.update(status="changes_requested", by=reviewer, at=now(), comment=comment)
                self._apply_amendment(run, text, reviewer)
                self._invalidate(run, node_id, "reviewer requested changes")
                run["status"] = "RUNNING"
            self._save(run)

    def request_stop(self, run_id: str, reason: str = "operator stop", by: str = "human") -> None:
        with self._lock(run_id):
            run = self._run(run_id)
            self._require_open(run)
            run["stop_requested"] = {"reason": reason, "by": by}
            self._emit(run_id, "stop_requested", f"human:{by}", None, {"reason": reason})
            self._save(run)

    # ====================================================================================
    # recovery
    # ====================================================================================
    def recover(self) -> list[str]:
        """Call once at startup. Roll back stages that were mid-flight when the process died."""
        resume: list[str] = []
        rows = self.db.query("SELECT id FROM runs WHERE status IN ('RUNNING','WAITING_INPUT','WAITING_APPROVAL')")
        for row in rows:
            run_id = row["id"]
            with self._lock(run_id):
                run = self._run(run_id)
                graph = self._graph(run)
                for nid, n in run["nodes"].items():
                    if n["status"] == "RUNNING":
                        self._rollback(run, graph.nodes[nid], "recovery after restart")
                        n["status"], n["token"] = "STALE", None
                        self._emit(run_id, "recovered", "system", nid,
                                   {"reason": "the process stopped while this stage was running; it will run again"})
                self._save(run)
                if run["status"] == "RUNNING":
                    resume.append(run_id)
        return resume
