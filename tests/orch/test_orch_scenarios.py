"""End-to-end: each scenario run once with the scripted reviewer, then inspected."""
import zipfile

import pytest
from orch_helpers import events


def run_of(scenario_runs, sid):
    engine, ids = scenario_runs
    return engine, ids[sid], engine.get_run(ids[sid])


def types(engine, rid):
    return [e["type"] for e in events(engine, rid)]


@pytest.mark.parametrize("sid", ["greenfield", "brownfield", "ambiguous"])
def test_required_scenarios_succeed_with_intact_audit(scenario_runs, sid):
    engine, rid, run = run_of(scenario_runs, sid)
    assert run["status"] == "SUCCEEDED", run["halt_reason"]
    assert engine.audit.verify(rid).ok


def test_drill_safe_stops(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "drill")
    assert run["status"] == "HALTED" and "policy_violation_budget_exceeded" in run["halt_reason"]
    assert engine.audit.verify(rid).ok  # a halted run's trail is still intact


def test_human_decisions_are_attributed_to_the_reviewer(scenario_runs):
    engine, rid, _ = run_of(scenario_runs, "greenfield")
    decisions = events(engine, rid, "approval_decision")
    assert [e["node_id"] for e in decisions] == ["design", "release_readiness"]
    assert {e["actor"] for e in decisions} == {"human:autopilot"}


# ---- greenfield -----------------------------------------------------------------------------
def test_greenfield_builds_a_service_that_passes_its_own_tests(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "greenfield")
    ver = run["nodes"]["verification"]["output"]
    assert ver["tests"]["failed"] == 0 and ver["tests"]["passed"] >= 40
    assert ver["conformance"]["missing"] == []
    assert "node_retry" in types(engine, rid)  # the injected docs fault was retried


def test_greenfield_release_bundle_contains_code_and_summary(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "greenfield")
    path = engine.run_dir(rid) / run["nodes"]["release_readiness"]["commit"]["path"]
    names = set(zipfile.ZipFile(path).namelist())
    assert {"shortener/api.py", "shortener/storage.py", "ENGINEERING_SUMMARY.md", "README.md"} <= names


def test_greenfield_tests_are_traceable_to_requirements(scenario_runs):
    _, _, run = run_of(scenario_runs, "greenfield")
    gates = {g["gate"]: g["passed"] for g in run["nodes"]["test_authoring"]["gates"]}
    assert gates == {"tests_traceable": True}


# ---- brownfield -----------------------------------------------------------------------------
def test_brownfield_replans_the_graph_when_the_design_has_a_schema_change(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "brownfield")
    replans = events(engine, rid, "graph_replanned")
    assert len(replans) == 1 and replans[0]["payload"]["added"] == ["migration_check"]
    assert run["nodes"]["migration_check"]["status"] == "SUCCEEDED"
    release = next(n for n in run["graph"]["nodes"] if n["id"] == "release_readiness")
    assert "migration_check" in release["deps"]


def test_brownfield_design_is_grounded_in_the_real_codebase(scenario_runs):
    _, _, run = run_of(scenario_runs, "brownfield")
    design = run["nodes"]["design"]["output"]
    assert design["codebase"]["existing"] is True
    assert any(r.startswith("POST /api/links ->") for r in design["codebase"]["routes"])
    assert "expires_at" not in design["codebase"]["schema"]["links"]
    expiry = next(e for e in design["impact"] if e["feature"] == "expiry")
    assert expiry["schema_change"]["add_columns"] == ["expires_at"]
    touched = {t["symbol"]: t["defined_in"] for t in expiry["touches"]}
    assert touched["resolve"] and touched["insert_link"]  # symbols located in the real modules


def test_brownfield_defect_is_caught_by_the_migration_probe_not_the_unit_tests(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "brownfield")
    failed = [e for e in events(engine, rid, "node_failed")]
    assert [(e["node_id"], e["payload"]["gate"]) for e in failed] == [("migration_check", "migration_ok")]
    # verification (fresh-database tests) never failed on the defective version
    assert run["nodes"]["verification"]["output"]["tests"]["failed"] == 0


def test_brownfield_existing_tests_still_pass(scenario_runs):
    _, _, run = run_of(scenario_runs, "brownfield")
    assert run["nodes"]["verification"]["output"]["tests"]["total"] >= 70  # 50 original + new


def test_brownfield_preserves_existing_files_it_was_not_scoped_to_change(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "brownfield")
    ws = engine.workspace(rid)
    assert "def get_link" in ws.read_text("shortener/storage.py")
    assert ws.exists("shortener/ratelimit.py") and ws.exists("tests/test_api.py")


# ---- ambiguous ------------------------------------------------------------------------------
def test_ambiguous_requirement_blocks_until_a_human_answers(scenario_runs):
    engine, rid, _ = run_of(scenario_runs, "ambiguous")
    t = types(engine, rid)
    assert t.index("waiting_input") < t.index("input_received") < t.index("approval_requested")


def test_amendment_replans_everything_downstream_of_requirements(scenario_runs):
    engine, rid, run = run_of(scenario_runs, "ambiguous")
    assert run["nodes"]["requirements"]["attempt"] == 3
    assert run["nodes"]["implementation"]["attempt"] == 2
    assert run["nodes"]["verification"]["output"]["tests"]["failed"] == 0
    assert run["amendments"] and "re-check the destination" in run["amendments"][0]["text"]
    # both approvals had to be given again for the new version
    approvals = [e["payload"]["decision"] for e in events(engine, rid, "approval_decision")]
    assert approvals == ["approve", "request_changes", "approve", "approve"]


def test_amendment_supersedes_the_earlier_decision_and_keeps_both_on_record(scenario_runs):
    _, _, run = run_of(scenario_runs, "ambiguous")
    enforce = [d for d in run["decisions"] if d["key"] == "answer:enforce"]
    assert [d["choice"] for d in enforce] == ["create_only", "create_and_redirect"]
    assert enforce[0]["superseded_by"] == enforce[1]["id"] and enforce[1]["supersedes"] == enforce[0]["id"]


def test_amendment_changes_the_shipped_behaviour(scenario_runs):
    engine, rid, _ = run_of(scenario_runs, "ambiguous")
    service = engine.workspace(rid).read_text("shortener/service.py")
    assert "blocked" in service.lower() or "safety" in service.lower()
    tests = "".join(engine.workspace(rid).read_text(f) for f in engine.workspace(rid).files(["tests/**"]))
    assert "redirect" in tests.lower()
