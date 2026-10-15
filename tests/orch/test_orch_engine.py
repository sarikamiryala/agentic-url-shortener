"""Engine behaviour: scheduling, the failure ladder, approvals, recovery. Real agents, wrapped to observe or disturb them."""
import threading
import time

import pytest

from orchestrator.agents.base import AgentOutput, AgentRefusal, TransientAgentError
from orchestrator.agents import default_registry
from orchestrator.engine import EngineError
from orchestrator.policy import Policy
from orchestrator.scenarios import SCENARIOS

from orch_helpers import Wrap, drive, events, new_run, wrapped_registry



def types(engine, rid):
    return [e["type"] for e in events(engine, rid)]


# ---- parallelism ----------------------------------------------------------------------------
def test_independent_stages_really_run_in_parallel(make_engine):
    """Three stages share only the design dependency. Each waits at a barrier for the other two, so the
    run can only finish if they are executing at the same time."""
    barrier = threading.Barrier(3, timeout=15)
    hold = {"before": lambda ctx: barrier.wait() if ctx.attempt == 1 and ctx.rework == 0 else None}
    eng = make_engine(wrapped_registry(implementation=hold, test_authoring=hold, docs=hold))
    rid = new_run(eng, clean=True)
    assert drive(eng, rid) == "SUCCEEDED"
    evs = events(eng, rid)
    started = [e["node_id"] for e in evs if e["type"] == "node_started"]
    after_design = started[started.index("design") + 1:][:3]
    assert set(after_design) == {"implementation", "test_authoring", "docs"}


def test_join_waits_for_every_dependency(make_engine):
    started = {}

    def mark(ctx):
        started[ctx.node_id] = time.monotonic()

    slow = {"before": lambda ctx: time.sleep(0.6)}
    eng = make_engine(wrapped_registry(docs=slow, release_readiness={"before": mark}))
    rid = new_run(eng, clean=True)
    drive(eng, rid)
    evs = events(eng, rid)
    docs_done = next(e["seq"] for e in evs if e["type"] == "node_succeeded" and e["node_id"] == "docs")
    release_started = next(e["seq"] for e in evs if e["type"] == "node_started" and e["node_id"] == "release_readiness")
    assert release_started > docs_done


# ---- retry, fallback, rework ----------------------------------------------------------------
def test_transient_failure_is_retried_with_rollback(make_engine):
    eng = make_engine()
    rid = new_run(eng, faults={"docs": {"transient": 1}})
    assert drive(eng, rid) == "SUCCEEDED"
    t = types(eng, rid)
    assert t.count("node_retry") == 1 and t.count("rollback") == 1
    assert eng.get_run(rid)["nodes"]["docs"]["attempt"] == 2


def test_retries_are_bounded_then_the_run_safe_stops(make_engine):
    eng = make_engine()
    rid = new_run(eng, faults={"docs": {"transient": 99}})
    assert drive(eng, rid) == "HALTED"
    run = eng.get_run(rid)
    assert run["halt_reason"].startswith("retries_exhausted:docs")
    assert types(eng, rid).count("node_retry") == 2  # default budget
    assert run["nodes"]["release_readiness"]["status"] == "PENDING"  # nothing downstream ran


def test_fallback_agent_takes_over_when_primary_keeps_failing(make_engine):
    def boom(ctx):
        raise TransientAgentError("primary unavailable")

    eng = make_engine(wrapped_registry(implementation={"before": boom}))
    rid = new_run(eng, clean=True)
    assert drive(eng, rid) == "SUCCEEDED"
    run = eng.get_run(rid)
    assert run["nodes"]["implementation"]["agent"] == "implementation.fallback"
    assert "node_fallback" in types(eng, rid)


def test_gate_failure_reworks_the_responsible_stage_with_feedback(make_engine):
    """Brownfield seeded defect: verification is fine on a fresh DB, the migration probe fails, and the
    failure goes back to implementation, which reworks with the feedback."""
    eng = make_engine()
    rid = new_run(eng, "brownfield")
    assert drive(eng, rid) == "SUCCEEDED"
    rework = events(eng, rid, "rework")
    assert len(rework) == 1 and rework[0]["node_id"] == "implementation" and rework[0]["payload"]["cause"] == "migration_check"
    run = eng.get_run(rid)
    assert run["nodes"]["implementation"]["rework"] == 1
    strategy = [d for d in run["decisions"] if d["key"] == "strategy"]
    assert [d["choice"] for d in strategy] == ["direct", "migration_aware"]
    assert strategy[0]["superseded_by"] == strategy[1]["id"]


def test_futile_rework_is_detected_and_escalated(make_engine):
    """An agent that returns the same failing artifact after being told why it failed is not retried again."""
    class Stuck:
        name = "stuck"

        def run(self, ctx):
            return AgentOutput(data={"pack": "x", "strategy": "none", "tasks_done": [], "files": []}, summary="nothing")

    reg = default_registry()
    reg.register("implementation", Stuck())
    reg.register("implementation.fallback", Stuck())
    eng = make_engine(reg)
    rid = new_run(eng, clean=True)
    assert drive(eng, rid) == "HALTED"
    assert eng.get_run(rid)["halt_reason"].startswith("rework_futile:implementation")
    assert "rework_futile" in types(eng, rid)


def test_policy_violation_is_blocked_rolled_back_and_budgeted(make_engine):
    eng = make_engine()
    rid = new_run(eng, "drill")
    assert drive(eng, rid) == "HALTED"
    run = eng.get_run(rid)
    assert "policy_violation_budget_exceeded" in run["halt_reason"]
    assert len(events(eng, rid, "policy_violation")) == 3
    ws = eng.workspace(rid)
    assert not ws.exists("shortener/_debug.py")  # the credential never reached disk
    assert not ws.files(["shortener/**"])  # partial work from the failed stage was rolled back


def test_agent_refusal_escalates_without_retrying(make_engine):
    def refuse(ctx):
        raise AgentRefusal("this needs an engineer")

    eng = make_engine(wrapped_registry(implementation={"before": refuse}))
    rid = new_run(eng, clean=True)
    assert drive(eng, rid) == "HALTED"
    run = eng.get_run(rid)
    assert run["halt_reason"].startswith("escalated_to_human:implementation")
    assert run["nodes"]["implementation"]["attempt"] == 1  # no retry, no fallback
    assert "node_retry" not in types(eng, rid)


# ---- guardrails -----------------------------------------------------------------------------
def test_change_freeze_blocks_stages_at_their_entry_gate(make_engine):
    eng = make_engine(policy=Policy(change_freeze=True))
    rid = new_run(eng, clean=True)
    assert drive(eng, rid) == "HALTED"
    assert eng.get_run(rid)["halt_reason"].startswith("entry_gate_failed:")
    assert any(e["payload"].get("gate") == "change_freeze_off" and not e["payload"]["passed"]
               for e in events(eng, rid, "gate_evaluated"))


def test_deadline_stops_the_run(make_engine):
    eng = make_engine(wrapped_registry(requirements={"before": lambda ctx: time.sleep(0.5)}),
                      policy=Policy(max_run_seconds=0.2))
    rid = new_run(eng, clean=True)
    assert drive(eng, rid) == "HALTED"
    assert eng.get_run(rid)["halt_reason"] == "deadline_exceeded"


def test_operator_stop_halts_the_run_and_is_attributed(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    assert eng.advance(rid) == "WAITING_APPROVAL"
    eng.request_stop(rid, "changed my mind", by="sarika")
    assert eng.advance(rid) == "HALTED"
    halted = events(eng, rid, "run_halted")[0]
    assert halted["actor"] == "human:sarika" and "changed my mind" in halted["payload"]["reason"]


def test_stop_discards_in_flight_work_and_rolls_it_back(make_engine):
    gate = threading.Event()

    def slow(ctx):
        ctx.workspace.write("shortener/partial.py", "x = 1\n")
        gate.wait(10)

    eng = make_engine(wrapped_registry(implementation={"before": slow}))
    rid = new_run(eng, clean=True)
    eng.advance(rid)  # to the design approval
    eng.decide_approval(rid, "design", "approve", reviewer="t")
    eng.advance_async(rid)
    deadline = time.time() + 10
    while time.time() < deadline and eng.get_run(rid)["nodes"]["implementation"]["status"] != "RUNNING":
        time.sleep(0.05)
    eng.request_stop(rid, "stop now")
    time.sleep(0.4)
    gate.set()
    for _ in range(100):
        if eng.get_run(rid)["status"] == "HALTED" and not eng._inflight.get(rid):
            break
        time.sleep(0.1)
    assert eng.get_run(rid)["status"] == "HALTED"
    time.sleep(0.5)
    assert not eng.workspace(rid).exists("shortener/partial.py")


# ---- approvals ------------------------------------------------------------------------------
def test_release_is_not_committed_before_approval(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    eng.advance(rid)
    eng.decide_approval(rid, "design", "approve", reviewer="t")
    assert eng.advance(rid) == "WAITING_APPROVAL"
    run = eng.get_run(rid)
    assert run["nodes"]["release_readiness"]["status"] == "AWAITING_APPROVAL"
    assert not (eng.run_dir(rid) / "release").exists()
    eng.decide_approval(rid, "release_readiness", "approve", reviewer="t")
    assert eng.get_run(rid)["nodes"]["release_readiness"]["commit"]["path"].endswith(".zip")


def test_rejection_safe_stops_and_nothing_is_released(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    eng.advance(rid)
    eng.decide_approval(rid, "design", "reject", reviewer="t", comment="wrong scope")
    assert eng.get_run(rid)["status"] == "HALTED"
    assert eng.get_run(rid)["halt_reason"].startswith("rejected_by:t:design")


def test_invalid_human_actions_are_refused(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    eng.advance(rid)
    with pytest.raises(EngineError, match="not awaiting"):
        eng.decide_approval(rid, "implementation", "approve")
    with pytest.raises(EngineError, match="decision must be"):
        eng.decide_approval(rid, "design", "maybe")
    with pytest.raises(EngineError, match="needs an amendment"):
        eng.decide_approval(rid, "design", "request_changes")
    with pytest.raises(EngineError, match="no clarification"):
        eng.provide_clarification(rid, {})


def test_approval_is_bound_to_the_reviewed_artifact(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    eng.advance(rid)
    run = eng._run(rid)
    run["nodes"]["design"]["output_digest"] = "0" * 64  # the artifact changed after approval was requested
    with pytest.raises(EngineError, match="changed since approval"):
        eng.decide_approval(rid, "design", "approve")


def test_actions_on_a_finished_run_are_refused(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    drive(eng, rid)
    with pytest.raises(EngineError):
        eng.request_stop(rid)


# ---- lineage and invalidation ---------------------------------------------------------------
def test_artifact_lineage_records_exact_inputs(make_engine):
    eng = make_engine()
    rid = new_run(eng, clean=True)
    drive(eng, rid)
    arts = {a["id"]: a for a in eng.get_run(rid)["artifacts"]}
    assert arts["verification@1"]["inputs"] == {"implementation": "implementation@1", "test_authoring": "test_authoring@1"}
    assert arts["design@1"]["inputs"] == {"requirements": "requirements@1"}
    for a in arts.values():
        assert set(a["inputs"].values()) <= set(arts)  # no dangling references


def test_changed_upstream_output_invalidates_only_dependents(make_engine):
    eng = make_engine()
    rid = new_run(eng, "ambiguous")
    from orchestrator.scenarios import AutoPilot
    AutoPilot(eng, SCENARIOS["ambiguous"].script).run(rid)
    run = eng.get_run(rid)
    assert run["status"] == "SUCCEEDED"
    n = run["nodes"]
    assert n["requirements"]["attempt"] == 3  # initial, after clarification, after amendment
    assert n["implementation"]["attempt"] == 2  # ran once before the amendment, once after
    assert n["security_review"]["attempt"] == 2
    # the amendment changed the code but security_review's artifact was identical, so release was not
    # invalidated a second time by it
    assert len(events(eng, rid, "output_unchanged")) >= 1


# ---- clarification --------------------------------------------------------------------------
def test_unsupported_clarification_option_does_not_unblock(make_engine):
    eng = make_engine()
    rid = new_run(eng, "ambiguous")
    assert eng.advance(rid) == "WAITING_INPUT"
    eng.provide_clarification(rid, {"threat": "service_abuse", "enforce": "create_only", "match": "exact_host"})
    assert eng.advance(rid) == "WAITING_INPUT"
    open_q = [q for q in eng.get_run(rid)["nodes"]["requirements"]["output"]["open_questions"] if not q["answered"]]
    assert [q["id"] for q in open_q] == ["threat"] and "cannot build" in open_q[0]["note"]


def test_nothing_buildable_asks_instead_of_guessing(make_engine):
    eng = make_engine()
    rid = new_run(eng, "ambiguous", requirement="Make it good.")
    assert eng.advance(rid) == "WAITING_INPUT"
    q = eng.get_run(rid)["nodes"]["requirements"]["output"]["open_questions"]
    assert q[0]["id"] == "intent"


def test_already_implemented_capability_is_not_rebuilt(make_engine):
    eng = make_engine()
    rid = new_run(eng, "ambiguous", requirement="Add rate limiting to link creation.")
    assert eng.advance(rid) == "WAITING_INPUT"
    q = eng.get_run(rid)["nodes"]["requirements"]["output"]["open_questions"][0]
    assert "Already implemented" in q["question"]


# ---- recovery -------------------------------------------------------------------------------
def test_recovery_rolls_back_a_stage_that_was_mid_flight(make_engine):
    e1 = make_engine(name="crash")
    rid = new_run(e1, clean=True)
    e1.advance(rid)
    e1.decide_approval(rid, "design", "approve", reviewer="t")
    # Simulate the process dying while implementation was running with partial output on disk.
    with e1._lock(rid):
        run = e1._run(rid)
        ws = e1.workspace(rid)
        n = run["nodes"]["implementation"]
        n["checkpoint"] = ws.snapshot(["shortener/**"], "implementation")
        (ws.root / "shortener").mkdir(exist_ok=True)
        (ws.root / "shortener/half_done.py").write_text("x = ")
        n["status"], n["token"] = "RUNNING", "tok-dead"
        e1._save(run)
    e1.close()

    from orchestrator.engine import Engine
    e2 = Engine(e1.data_dir)
    e2.raise_internal_errors = True
    try:
        assert e2.recover() == [rid]
        assert not e2.workspace(rid).exists("shortener/half_done.py")
        assert any(e["type"] == "recovered" and e["node_id"] == "implementation" for e in events(e2, rid))
        assert drive(e2, rid) == "SUCCEEDED"
        assert e2.audit.verify(rid).ok
    finally:
        e2.close()


def test_relative_data_directory_works(tmp_path, monkeypatch):
    """The default data directory is relative. Stages run subprocesses elsewhere, so paths must be absolute."""
    from orchestrator.engine import Engine

    monkeypatch.chdir(tmp_path)
    eng = Engine("data/orchestrator")
    eng.raise_internal_errors = True
    try:
        assert eng.data_dir.is_absolute()
        rid = new_run(eng, clean=True)
        assert drive(eng, rid) == "SUCCEEDED"
    finally:
        eng.close()
