"""Entry and exit gates.

A gate is a named predicate over evidence (agent output, workspace contents, upstream results).
Gates, not agents, decide whether a stage may start or has finished. Where possible they
re-check the workspace themselves rather than trusting an agent's claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .analysis import scan_codebase
from .graph import Graph, NodeSpec
from .policy import Policy
from .workspace import Workspace


@dataclass
class GateResult:
    passed: bool
    detail: str = ""
    kind: str = "fail"  # "fail" | "needs_input"


@dataclass
class GateContext:
    run: dict
    spec: NodeSpec
    graph: Graph
    output: dict | None
    ws: Workspace
    policy: Policy

    def upstream(self, node_id: str) -> dict:
        return self.run["nodes"][node_id]["output"] or {}


GateFn = Callable[[GateContext], GateResult]
GATES: dict[str, GateFn] = {}


def gate(name: str) -> Callable[[GateFn], GateFn]:
    def deco(fn: GateFn) -> GateFn:
        GATES[name] = fn
        return fn

    return deco


def evaluate(name: str, ctx: GateContext) -> GateResult:
    fn = GATES.get(name)
    if fn is None:
        return GateResult(False, f"unknown gate '{name}'")
    try:
        return fn(ctx)
    except Exception as exc:  # a crashing gate must fail closed
        return GateResult(False, f"gate crashed: {type(exc).__name__}: {exc}")


def _ok(detail: str = "") -> GateResult:
    return GateResult(True, detail)


def _fail(detail: str) -> GateResult:
    return GateResult(False, detail)


# ---- entry gates -----------------------------------------------------------------------
@gate("change_freeze_off")
def change_freeze_off(c: GateContext) -> GateResult:
    return _fail("a change freeze is active in the policy") if c.policy.change_freeze else _ok()


@gate("workspace_has_code")
def workspace_has_code(c: GateContext) -> GateResult:
    n = len(c.ws.files(["shortener/**/*.py"]))
    return _ok(f"{n} source files") if n else _fail("there is no source code to check")


@gate("upstream_green")
def upstream_green(c: GateContext) -> GateResult:
    bad = [d for d in c.spec.deps if c.upstream(d).get("ok") is False]
    return _fail(f"upstream stage(s) not green: {', '.join(bad)}") if bad else _ok()


# ---- requirements ------------------------------------------------------------------------
@gate("no_blocking_questions")
def no_blocking_questions(c: GateContext) -> GateResult:
    open_q = [q["id"] for q in c.output["open_questions"] if q["blocking"] and not q["answered"]]
    if open_q:
        return GateResult(False, f"{len(open_q)} question(s) need a human answer: {', '.join(open_q)}", "needs_input")
    return _ok("no open blocking questions")


@gate("spec_complete")
def spec_complete(c: GateContext) -> GateResult:
    out = c.output
    if not out["functional"]:
        return _fail("no functional requirements were identified")
    thin = [fr["id"] for fr in out["functional"] if not fr["acceptance"]]
    if thin:
        return _fail(f"requirements without acceptance criteria: {', '.join(thin)}")
    return _ok(f"{len(out['functional'])} requirement(s), all with acceptance criteria")


# ---- design --------------------------------------------------------------------------------
@gate("design_complete")
def design_complete(c: GateContext) -> GateResult:
    d = c.output
    missing = [k for k in ("contract", "tasks", "risks", "impact_tags") if not d.get(k)]
    if missing or not d["contract"].get("endpoints"):
        return _fail(f"design is missing: {', '.join(missing) or 'contract endpoints'}")
    ids = [t["id"] for t in d["tasks"]]
    if len(ids) != len(set(ids)):
        return _fail("duplicate task ids")
    return _ok(f"{len(d['tasks'])} tasks, {len(d['risks'])} risks")


@gate("tasks_acyclic")
def tasks_acyclic(c: GateContext) -> GateResult:
    tasks = {t["id"]: t for t in c.output["tasks"]}
    for t in tasks.values():
        unknown = [d for d in t["depends_on"] if d not in tasks]
        if unknown:
            return _fail(f"task {t['id']} depends on unknown task(s) {unknown}")
    indeg = {i: len(t["depends_on"]) for i, t in tasks.items()}
    ready = [i for i, n in indeg.items() if n == 0]
    seen = 0
    while ready:
        cur = ready.pop()
        seen += 1
        for i, t in tasks.items():
            if cur in t["depends_on"]:
                indeg[i] -= 1
                if indeg[i] == 0:
                    ready.append(i)
    return _ok("task graph is acyclic") if seen == len(tasks) else _fail("task dependencies contain a cycle")


@gate("risks_have_validation")
def risks_have_validation(c: GateContext) -> GateResult:
    problems = []
    for r in c.output["risks"]:
        vb = r["validated_by"]
        if vb == "accepted":
            if not r.get("note"):
                problems.append(f"{r['id']}: accepted risk needs a note")
        elif vb not in c.graph.nodes:
            problems.append(f"{r['id']}: validated_by '{vb}' is not a stage in this run")
    return _fail("; ".join(problems)) if problems else _ok("every risk is validated by a stage or explicitly accepted")


# ---- implementation ------------------------------------------------------------------------
@gate("files_written")
def files_written(c: GateContext) -> GateResult:
    files = c.output.get("files", [])
    if not files:
        return _fail("the stage wrote no files")
    missing = [f["path"] for f in files if not c.ws.exists(f["path"])]
    return _fail(f"files missing from the workspace: {missing}") if missing else _ok(f"{len(files)} files present")


@gate("compiles")
def compiles(c: GateContext) -> GateResult:
    for rel in c.ws.files(c.spec.write_scope):
        if rel.endswith(".py"):
            try:
                compile(c.ws.read_text(rel), rel, "exec")
            except SyntaxError as exc:
                return _fail(f"{rel}:{exc.lineno}: {exc.msg}")
    return _ok("all Python files compile")


@gate("policy_clean")
def policy_clean(c: GateContext) -> GateResult:
    for rel in c.ws.files(c.spec.write_scope):
        found = c.policy.scan_secrets(rel, c.ws.read_text(rel))
        if found:
            return _fail(f"{rel}: {found[0].detail}")
    return _ok("independent re-scan found no secrets")


@gate("tasks_covered")
def tasks_covered(c: GateContext) -> GateResult:
    design = c.upstream("design")
    wanted = {t["module"] for t in design["tasks"] if t["stage"] == "implementation"}
    written = {f["path"] for f in c.output["files"]}
    missing = sorted(wanted - written)
    return _fail(f"no output for module(s): {missing}") if missing else _ok(f"all {len(wanted)} planned modules produced")


# ---- tests / docs ------------------------------------------------------------------------------
@gate("tests_traceable")
def tests_traceable(c: GateContext) -> GateResult:
    out = c.output
    for f in out["files"]:
        try:
            compile(c.ws.read_text(f["path"]), f["path"], "exec")
        except SyntaxError as exc:
            return _fail(f"{f['path']}:{exc.lineno}: {exc.msg}")
    if out["test_functions"] == 0:
        return _fail("no test functions were written")
    covered = {x for t in scan_codebase(c.ws.root)["tests"] for x in t["covers"]}
    requested = set(c.upstream("design")["requested_features"])
    missing = sorted(requested - covered)
    if missing:
        return _fail(f"requested features without tests: {missing}")
    return _ok(f"{out['test_functions']} tests; every requested feature is covered")


@gate("docs_cover_contract")
def docs_cover_contract(c: GateContext) -> GateResult:
    expected = {f"{e['method']} {e['path']}" for e in c.upstream("design")["contract"]["endpoints"]}
    missing = sorted(expected - set(c.output["documented_endpoints"]))
    if missing:
        return _fail(f"undocumented endpoints: {missing}")
    text = c.ws.read_text("docs/API.md") if c.ws.exists("docs/API.md") else ""
    absent = sorted(e for e in expected if e.split(" ", 1)[1] not in text)
    return _fail(f"docs/API.md does not mention: {absent}") if absent else _ok(f"{len(expected)} endpoints documented")


# ---- checkers ------------------------------------------------------------------------------------
@gate("no_high_findings")
def no_high_findings(c: GateContext) -> GateResult:
    high = [f for f in c.output["findings"] if f["severity"] == "high"]
    if high:
        f = high[0]
        return _fail(f"{len(high)} high finding(s); first: {f['rule']} in {f['path']}:{f['line']} ({f['detail']})")
    return _ok(f"no high findings in {c.output['files_scanned']} files")


@gate("migration_ok")
def migration_ok(c: GateContext) -> GateResult:
    bad = [x for x in c.output["checks"] if not x["ok"]]
    if bad:
        return _fail(f"{bad[0]['name']}: {bad[0]['detail']}")
    return _ok(f"{len(c.output['checks'])} upgrade probes passed")


@gate("tests_pass")
def tests_pass(c: GateContext) -> GateResult:
    t = c.output["tests"]
    if t["total"] == 0:
        why = c.output["failures"][0]["message"] if c.output["failures"] else ""
        return _fail(f"no tests ran: {why}" if why else "no tests ran")
    if t["failed"] or t["errors"]:
        first = c.output["failures"][0] if c.output["failures"] else {"name": "?", "message": ""}
        return _fail(f"{t['failed']} failed, {t['errors']} errors; first: {first['name']}: {first['message']}")
    return _ok(f"{t['passed']}/{t['total']} passed")


@gate("contract_conformance")
def contract_conformance(c: GateContext) -> GateResult:
    conf = c.output["conformance"]
    if conf["error"]:
        return _fail(f"could not inspect the running app: {conf['error']}")
    if conf["missing"]:
        return _fail(f"contract endpoints missing from the app: {conf['missing']}")
    return _ok(f"{len(conf['expected'])} contract endpoints present")


@gate("release_checklist")
def release_checklist(c: GateContext) -> GateResult:
    bad = [x["item"] for x in c.output["checklist"] if not x["ok"]]
    return _fail(f"checklist items failing: {bad}") if bad else _ok(f"{len(c.output['checklist'])} checklist items green")
