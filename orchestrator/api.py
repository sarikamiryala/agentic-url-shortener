"""HTTP API and dashboard for the orchestrator.

Reads are open. Anything that starts work or records a human decision goes through
`require_admin`, which checks `X-Admin-Token` when ADMIN_TOKEN is set (set it on any public
deployment). Starting runs is also rate limited and capped, because every run executes a
pytest subprocess.
"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from shortener.ratelimit import RateLimiter

from . import metrics
from .engine import TERMINAL, Engine, EngineError
from .graph import Graph
from .scenarios import SCENARIOS, start_scenario

STATIC = Path(__file__).parent / "static"
MAX_FILE_BYTES = 200_000


class StartRun(BaseModel):
    scenario: str
    autopilot: bool = True
    clean: bool = False
    requirement: str | None = Field(default=None, max_length=1000)


class Clarify(BaseModel):
    answers: dict[str, str] = {}
    use_defaults: bool = False
    by: str = Field(default="reviewer", max_length=40)


class Decision(BaseModel):
    decision: str
    comment: str = Field(default="", max_length=1000)
    amendment: str | None = Field(default=None, max_length=1000)
    by: str = Field(default="reviewer", max_length=40)


class Amend(BaseModel):
    text: str = Field(max_length=1000)
    by: str = Field(default="reviewer", max_length=40)


class Stop(BaseModel):
    reason: str = Field(default="operator stop", max_length=300)
    by: str = Field(default="reviewer", max_length=40)


def run_view(run: dict) -> dict:
    """The run as the dashboard needs it: no bulky outputs, plus what is waiting on a human."""
    graph = Graph.from_dict(run["graph"])
    nodes = {}
    for nid, n in run["nodes"].items():
        spec = graph.nodes[nid]
        nodes[nid] = {
            "id": nid, "title": spec.title, "status": n["status"], "agent": n["agent"],
            "deps": spec.deps, "write_scope": spec.write_scope, "entry_gates": spec.entry_gates,
            "exit_gates": spec.exit_gates, "approval_rule": spec.approval, "dynamic": spec.dynamic,
            "max_retries": spec.max_retries, "max_rework": spec.max_rework, "fallback_agent": spec.fallback_agent,
            "attempt": n["attempt"], "retries": n["retries"], "rework": n["rework"],
            "fallback_active": n["fallback_active"], "summary": n["summary"], "gates": n["gates"],
            "failure": n["failure"], "artifact_id": n["artifact_id"], "approval": n["approval"],
            "duration_s": n["last_exec_s"], "commit": n["commit"],
        }
    req = run["nodes"]["requirements"]
    questions = []
    if req["status"] == "WAITING_INPUT" and req["output"]:
        questions = [q for q in req["output"]["open_questions"] if q["blocking"] and not q["answered"]]
    return {
        "id": run["id"], "scenario": run["scenario"], "title": run["title"], "status": run["status"],
        "mode": run["mode"], "requirement": run["requirement"], "halt_reason": run["halt_reason"],
        "created_at": run["created_at"], "finished_at": run["finished_at"], "config": run["config"],
        "faults": run["faults"], "amendments": run["amendments"], "answers": run["answers"],
        "graph": {"layers": graph.layers(), "edges": [list(e) for e in graph.edges()]},
        "nodes": nodes, "artifacts": run["artifacts"], "decisions": run["decisions"],
        "approvals_log": run["approvals_log"], "open_questions": questions,
        "pending_approvals": [nid for nid, n in run["nodes"].items() if n["status"] == "AWAITING_APPROVAL"],
        "stop_requested": run["stop_requested"],
    }


def create_app(engine: Engine | None = None, data_dir: str | None = None) -> FastAPI:
    owns_engine = engine is None
    engine = engine or Engine(data_dir or os.getenv("ORCH_DATA_DIR", "data/orchestrator"))
    admin_token = os.getenv("ADMIN_TOKEN", "")
    max_active = int(os.getenv("ORCH_MAX_ACTIVE_RUNS", "3"))
    start_limiter = RateLimiter(int(os.getenv("ORCH_RUNS_PER_MINUTE", "10")), 60.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for run_id in engine.recover():
            engine.advance_async(run_id)
        yield
        if owns_engine:
            engine.close()

    app = FastAPI(title="Agentic SE Orchestrator", version="1.0.0", lifespan=lifespan)
    app.state.engine = engine

    def require_admin(request: Request) -> None:
        if admin_token and not hmac.compare_digest(request.headers.get("x-admin-token", ""), admin_token):
            raise HTTPException(status_code=401, detail="admin token required")

    def get_run(run_id: str) -> dict:
        try:
            return engine.get_run(run_id)
        except EngineError as exc:
            raise HTTPException(status_code=404, detail="unknown run") from exc

    def human_action(fn, *args, **kwargs) -> dict:
        try:
            fn(*args, **kwargs)
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        engine.advance_async(args[0])
        return {"ok": True}

    # ---- reads -------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"))

    @app.get("/api/config")
    def config() -> dict:
        return {"admin_token_required": bool(admin_token), "max_active_runs": max_active}

    @app.get("/api/scenarios")
    def scenarios() -> list[dict]:
        return [s.public() for s in SCENARIOS.values()]

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return engine.list_runs()

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        return run_view(get_run(run_id))

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, since: int = 0) -> list[dict]:
        get_run(run_id)
        return engine.audit.events(run_id, since)

    @app.get("/api/runs/{run_id}/audit/verify")
    def audit_verify(run_id: str) -> dict:
        get_run(run_id)
        return engine.audit.verify(run_id).to_dict()

    @app.get("/api/runs/{run_id}/metrics")
    def run_metrics(run_id: str) -> dict:
        run = get_run(run_id)
        return metrics.run_metrics(engine.audit.events(run_id), run)

    @app.get("/api/metrics")
    def all_metrics() -> dict:
        return metrics.global_metrics(engine.db, engine.audit)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id:path}")
    def artifact(run_id: str, artifact_id: str) -> dict:
        run = get_run(run_id)
        meta = next((a for a in run["artifacts"] if a["id"] == artifact_id), None)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown artifact")
        n = run["nodes"].get(meta["node"])
        current = bool(n and n["artifact_id"] == artifact_id)
        return {**meta, "current": current,
                "data": n["output"] if current else None,
                "note": None if current else "Superseded. Only the latest output of each stage is retained; "
                                             "its digest and inputs remain in the lineage."}

    @app.get("/api/runs/{run_id}/lineage")
    def lineage(run_id: str) -> dict:
        run = get_run(run_id)
        return {
            "artifacts": [{k: a[k] for k in ("id", "node", "version", "digest", "inputs")} for a in run["artifacts"]],
            "decisions": run["decisions"],
        }

    @app.get("/api/runs/{run_id}/files")
    def files(run_id: str) -> list[str]:
        get_run(run_id)
        return engine.workspace(run_id).files()

    @app.get("/api/runs/{run_id}/file", response_class=PlainTextResponse)
    def file_content(run_id: str, path: str) -> str:
        get_run(run_id)
        ws = engine.workspace(run_id)
        if path not in ws.files():
            raise HTTPException(status_code=404, detail="no such file")
        text = ws.read_text(path)
        return text[:MAX_FILE_BYTES]

    @app.get("/api/runs/{run_id}/diff", response_class=PlainTextResponse)
    def diff(run_id: str) -> str:
        run = get_run(run_id)
        cp = run.get("baseline_checkpoint")
        if not cp:
            raise HTTPException(status_code=404, detail="no baseline for this run")
        return engine.workspace(run_id).diff_since(cp) or "(no changes)"

    @app.get("/api/runs/{run_id}/summary", response_class=PlainTextResponse)
    def summary(run_id: str) -> str:
        get_run(run_id)
        reports = engine.run_dir(run_id) / "reports"
        for name in ("engineering_summary.md", "engineering_summary.draft.md"):
            p = reports / name
            if p.is_file():
                return p.read_text(encoding="utf-8")
        raise HTTPException(status_code=404, detail="the summary is produced by the release stage")

    @app.get("/api/runs/{run_id}/bundle")
    def bundle(run_id: str) -> FileResponse:
        run = get_run(run_id)
        commit = run["nodes"].get("release_readiness", {}).get("commit")
        path = engine.run_dir(run_id) / commit["path"] if commit else None
        if not path or not path.is_file():
            raise HTTPException(status_code=404, detail="no release bundle: release is committed only after approval")
        return FileResponse(path, filename=path.name, media_type="application/zip")

    # ---- actions -----------------------------------------------------------------------
    @app.post("/api/runs", status_code=202, dependencies=[Depends(require_admin)])
    def start_run(body: StartRun, request: Request) -> dict:
        if body.scenario not in SCENARIOS:
            raise HTTPException(status_code=404, detail="unknown scenario")
        client = request.client.host if request.client else "unknown"
        allowed, retry_after = start_limiter.check(client)
        if not allowed:
            raise HTTPException(status_code=429, detail="too many runs started", headers={"Retry-After": str(retry_after)})
        if engine.count_active() >= max_active:
            raise HTTPException(status_code=429, detail=f"{max_active} runs are already active; wait for one to finish or stop it")
        run_id = start_scenario(engine, body.scenario, clean=body.clean, requirement=body.requirement,
                                autopilot=body.autopilot)
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/clarify", dependencies=[Depends(require_admin)])
    def clarify(run_id: str, body: Clarify) -> dict:
        return human_action(engine.provide_clarification, run_id, body.answers, by=body.by,
                            use_defaults=body.use_defaults)

    @app.post("/api/runs/{run_id}/approvals/{node_id}", dependencies=[Depends(require_admin)])
    def approve(run_id: str, node_id: str, body: Decision) -> dict:
        return human_action(engine.decide_approval, run_id, node_id, body.decision, reviewer=body.by,
                            comment=body.comment, amendment=body.amendment)

    @app.post("/api/runs/{run_id}/amend", dependencies=[Depends(require_admin)])
    def amend(run_id: str, body: Amend) -> dict:
        return human_action(engine.amend, run_id, body.text, by=body.by)

    @app.post("/api/runs/{run_id}/stop", dependencies=[Depends(require_admin)])
    def stop(run_id: str, body: Stop) -> dict:
        return human_action(engine.request_stop, run_id, reason=body.reason, by=body.by)

    return app
