"""Scenario definitions and the scripted reviewer ("autopilot").

The three required scenarios plus one guardrail drill. A scenario is a requirement, a starting
codebase, optional fault injection, and a script of human responses. The script stands in for a
person so runs are reproducible; the dashboard lets a real person take those decisions instead,
and every scripted decision is recorded in the audit log under the actor `human:autopilot`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .catalog import REPO_ROOT
from .engine import TERMINAL, Engine


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    kind: str  # greenfield | brownfield | ambiguous | drill
    description: str
    requirement: str
    mode: str
    seed: str | None
    faults: dict = field(default_factory=dict)
    script: tuple = ()
    demonstrates: tuple = ()

    def public(self) -> dict:
        return {
            "id": self.id, "title": self.title, "kind": self.kind, "description": self.description,
            "requirement": self.requirement, "mode": self.mode, "faults": self.faults,
            "demonstrates": list(self.demonstrates),
        }


SCENARIOS: dict[str, Scenario] = {
    "greenfield": Scenario(
        id="greenfield",
        title="1. Greenfield: build the URL shortener",
        kind="greenfield",
        description=(
            "A well-defined requirement for a new system. A transient failure is injected into the docs agent "
            "after it has written a file, to show bounded retry with rollback."
        ),
        requirement=(
            "Build a URL shortener service. Users can create short links for long URLs, anyone can follow a "
            "short link and get redirected, and link owners can see click analytics. Validate input and "
            "rate-limit link creation."
        ),
        mode="greenfield",
        seed=None,
        faults={"docs": {"transient": 1}},
        script=(
            {"on": "approve:design", "decision": "approve", "comment": "Architecture and task plan look right."},
            {"on": "approve:release_readiness", "decision": "approve", "comment": "Checklist is green."},
        ),
        demonstrates=("decomposition", "parallel paths + join", "entry/exit gates", "retry + rollback",
                      "approval checkpoints", "audit trail"),
    ),
    "brownfield": Scenario(
        id="brownfield",
        title="2. Brownfield: add custom aliases and expiry",
        kind="brownfield",
        description=(
            "An enhancement to the existing service. The design agent analyses the real codebase; the schema "
            "change makes the planner insert a migration check at runtime. The primary implementation agent "
            "ships a seeded defect (no in-place migration) that fresh-database tests cannot see."
        ),
        requirement=(
            "Extend the existing URL shortener: let users choose a custom alias for a short link, and let "
            "links expire after a number of days. Existing links and data must keep working."
        ),
        mode="brownfield",
        seed="baseline",
        faults={"implementation": {"seeded_defect": True}},
        script=(
            {"on": "approve:design", "decision": "approve", "comment": "Additive schema change is acceptable."},
            {"on": "approve:release_readiness", "decision": "approve", "comment": "Migration probe passed."},
        ),
        demonstrates=("codebase reasoning", "impact analysis", "dynamic re-plan (graph change)", "gate failure -> rework",
                      "rollback", "fallback agent", "migration safety"),
    ),
    "ambiguous": Scenario(
        id="ambiguous",
        title="3. Ambiguous: \"Make links safer\"",
        kind="ambiguous",
        description=(
            "A vague requirement. The system refuses to guess, asks blocking questions, and then a reviewer "
            "changes the requirement at the final review, which re-plans the whole chain."
        ),
        requirement="Make links safer.",
        mode="brownfield",
        seed="baseline",
        faults={},
        script=(
            {"on": "clarify", "answers": {"threat": "malicious_destinations", "enforce": "create_only",
                                          "match": "host_and_subdomains"}},
            {"on": "approve:design", "decision": "approve", "comment": "Creation-time blocklist is a reasonable start."},
            {"on": "approve:release_readiness", "decision": "request_changes",
             "comment": "The accepted risk about already-created links worries me.",
             "amendment": "Links created before a domain was blocklisted must also stop working: "
                          "re-check the destination each time a link is followed."},
            {"on": "approve:design", "decision": "approve", "comment": "Redirect-time check closes the gap."},
            {"on": "approve:release_readiness", "decision": "approve", "comment": "Good to ship."},
        ),
        demonstrates=("ambiguity detection", "blocking clarification", "unsupported-option escalation",
                      "decision lineage + supersession", "re-plan on changed requirements", "early cutoff"),
    ),
    "drill": Scenario(
        id="drill",
        title="Drill: guardrails, rollback and safe-stop",
        kind="drill",
        description=(
            "Fault injection: every implementation agent tries to commit a hardcoded credential. The policy "
            "engine blocks each write, partial work is rolled back, and the run safe-stops when the "
            "recovery budget is spent."
        ),
        requirement=(
            "Build a URL shortener service. Users can create short links for long URLs, anyone can follow a "
            "short link and get redirected, and link owners can see click analytics. Validate input and "
            "rate-limit link creation."
        ),
        mode="greenfield",
        seed=None,
        faults={"implementation": {"secret_leak": {"agents": ["primary", "fallback"]}}},
        script=({"on": "approve:design", "decision": "approve", "comment": "Plan approved."},),
        demonstrates=("policy guardrails", "rollback", "bounded recovery", "safe-stop"),
    ),
}


def baseline_seed(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """The 'existing codebase' for brownfield runs: the v1 service and its tests."""
    files: dict[str, str] = {}
    for p in sorted((repo_root / "shortener").glob("*.py")):
        files[f"shortener/{p.name}"] = p.read_text(encoding="utf-8")
    for p in sorted((repo_root / "tests" / "product").glob("*.py")):
        files[f"tests/{p.name}"] = p.read_text(encoding="utf-8")
    return files


def start_scenario(engine: Engine, scenario_id: str, *, clean: bool = False, requirement: str | None = None,
                   autopilot: bool = False) -> str:
    sc = SCENARIOS[scenario_id]
    faults = {} if clean else sc.faults
    run_id = engine.create_run(
        scenario=sc.id, title=sc.title, requirement=requirement or sc.requirement, mode=sc.mode,
        seed_files=baseline_seed(engine.repo_root) if sc.seed == "baseline" else None,
        faults=faults, config={"autopilot": autopilot, "clean": clean},
    )
    if autopilot:
        engine.submit(AutoPilot(engine, sc.script).run, run_id)
    else:
        engine.advance_async(run_id)
    return run_id


class AutoPilot:
    """A scripted reviewer. Each script step is consumed once, in order, when the run blocks on it."""

    def __init__(self, engine: Engine, script: tuple, by: str = "autopilot", max_steps: int = 60) -> None:
        self.engine, self.script, self.by, self.max_steps = engine, list(script), by, max_steps
        self._used: set[int] = set()

    def _next(self, run: dict) -> tuple[int, dict, str] | None:
        if run["status"] == "WAITING_INPUT":
            trigger = "clarify"
        elif run["status"] == "WAITING_APPROVAL":
            waiting = sorted(nid for nid, n in run["nodes"].items() if n["status"] == "AWAITING_APPROVAL")
            if not waiting:
                return None
            trigger = f"approve:{waiting[0]}"
        else:
            return None
        for i, step in enumerate(self.script):
            if i not in self._used and step["on"] == trigger:
                return i, step, trigger
        return None

    def run(self, run_id: str) -> str:
        status = "RUNNING"
        for _ in range(self.max_steps):
            status = self.engine.advance(run_id)
            if status in TERMINAL:
                return status
            nxt = self._next(self.engine.get_run(run_id))
            if nxt is None:
                return status  # blocked on a human and the script has nothing for it
            i, step, trigger = nxt
            self._used.add(i)
            if trigger == "clarify":
                self.engine.provide_clarification(run_id, step["answers"], by=self.by)
            else:
                self.engine.decide_approval(
                    run_id, trigger.split(":", 1)[1], step["decision"], reviewer=self.by,
                    comment=step.get("comment", ""), amendment=step.get("amendment"),
                )
        return status
