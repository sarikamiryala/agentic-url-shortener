"""Unit tests for the orchestrator's building blocks."""
import ast
import sqlite3

import pytest

from orchestrator import metrics
from orchestrator.agents.checkers import scan_python
from orchestrator.analysis import scan_codebase
from orchestrator.audit import AuditLog
from orchestrator.db import Database
from orchestrator.graph import Graph, GraphError, NodeSpec
from orchestrator.pipeline import apply_adjustments, build_graph
from orchestrator.policy import Policy, PolicyViolation
from orchestrator.scenarios import baseline_seed
from orchestrator.util import glob_match
from orchestrator.workspace import Workspace


# ---- graph ----------------------------------------------------------------------------------
def diamond():
    return Graph([NodeSpec("a", "A", "a"), NodeSpec("b", "B", "b", deps=["a"]),
                  NodeSpec("c", "C", "c", deps=["a"]), NodeSpec("d", "D", "d", deps=["b", "c"])])


def test_layers_group_independent_nodes():
    assert diamond().layers() == [["a"], ["b", "c"], ["d"]]


def test_descendants_and_ancestors():
    g = diamond()
    assert g.descendants("a") == {"b", "c", "d"}
    assert g.ancestors("d") == {"a", "b", "c"}


def test_cycle_is_rejected():
    with pytest.raises(GraphError, match="cycle"):
        Graph([NodeSpec("a", "A", "a", deps=["b"]), NodeSpec("b", "B", "b", deps=["a"])])


def test_unknown_dependency_is_rejected():
    with pytest.raises(GraphError, match="unknown"):
        Graph([NodeSpec("a", "A", "a", deps=["ghost"])])


def test_graph_round_trips_through_dict():
    g = build_graph(Policy())
    assert Graph.from_dict(g.to_dict()).edges() == g.edges()


def test_pipeline_has_parallel_branches():
    layers = build_graph(Policy()).layers()
    assert {"implementation", "test_authoring", "docs"} <= set(layers[2])


def test_replan_adds_and_removes_migration_node():
    g = build_graph(Policy())
    design = {"impact_tags": ["schema_change"], "mode": "brownfield"}
    assert apply_adjustments(g, design, Policy())["added"] == ["migration_check"]
    assert "migration_check" in g.nodes["release_readiness"].deps
    assert apply_adjustments(g, {"impact_tags": [], "mode": "brownfield"}, Policy())["removed"] == ["migration_check"]
    assert "migration_check" not in g.nodes["release_readiness"].deps


def test_replan_ignores_schema_change_in_greenfield():
    g = build_graph(Policy())
    assert apply_adjustments(g, {"impact_tags": ["schema_change"], "mode": "greenfield"}, Policy())["added"] == []


# ---- policy ---------------------------------------------------------------------------------
def test_secret_scanner_flags_known_patterns():
    p = Policy()
    assert p.scan_secrets("x.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"')
    assert p.scan_secrets("x.py", "-----BEGIN RSA PRIVATE KEY-----")
    assert p.scan_secrets("x.py", 'api_key = "abcdefghijklmnop1234"')
    assert not p.scan_secrets("x.py", "api_key = os.environ['API_KEY']")


def test_protected_paths_and_approval_tags():
    p = Policy()
    assert p.is_protected("policy.json") and p.is_protected(".github/workflows/ci.yml")
    assert not p.is_protected("shortener/api.py")
    assert p.requires_approval(["schema_change", "other"]) == ["schema_change"]
    assert p.requires_approval(["other"]) == []


def test_glob_match_double_star():
    assert glob_match("shortener/a/b.py", "shortener/**")
    assert not glob_match("tests/a.py", "shortener/**")


# ---- workspace ------------------------------------------------------------------------------
@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "w", tmp_path / "cp", Policy())


def test_write_inside_scope_is_allowed_and_recorded(ws):
    events = []
    s = ws.scoped("impl", ["shortener/**"], "agent:x", emit=lambda *a: events.append(a))
    s.write("shortener/a.py", "x = 1\n")
    assert ws.read_text("shortener/a.py") == "x = 1\n"
    assert events[-1][0] == "file_written"


@pytest.mark.parametrize("path,rule", [
    ("tests/a.py", "write_scope"),
    ("../escape.py", "path_traversal"),
    ("/etc/passwd", "path_traversal"),
])
def test_out_of_scope_writes_are_blocked(ws, path, rule):
    s = ws.scoped("impl", ["shortener/**"], "agent:x")
    with pytest.raises(PolicyViolation) as exc:
        s.write(path, "x")
    assert rule in {v.rule for v in exc.value.violations}


def test_protected_path_blocked_even_when_in_scope(ws):
    s = ws.scoped("any", ["**"], "agent:x")
    with pytest.raises(PolicyViolation) as exc:
        s.write("policy.json", "{}")
    assert exc.value.violations[0].rule == "change_control"


def test_secret_never_reaches_disk(ws):
    s = ws.scoped("impl", ["shortener/**"], "agent:x")
    with pytest.raises(PolicyViolation):
        s.write("shortener/k.py", 'K = "AKIAIOSFODNN7EXAMPLE"')
    assert not ws.exists("shortener/k.py")


def test_violations_are_emitted(ws):
    seen = []
    s = ws.scoped("impl", ["shortener/**"], "agent:x", emit=lambda *a: seen.append(a[0]))
    with pytest.raises(PolicyViolation):
        s.write("tests/a.py", "x")
    assert seen == ["policy_violation"]


def test_size_budget(tmp_path):
    w = Workspace(tmp_path / "w", tmp_path / "cp", Policy(max_files_per_stage=2))
    s = w.scoped("impl", ["**"], "agent:x")
    s.write("a.py", "1")
    s.write("b.py", "2")
    with pytest.raises(PolicyViolation, match="size_budget"):
        s.write("c.py", "3")


def test_restore_is_scope_aware(ws):
    ws.seed({"a/x.txt": "one", "b/y.txt": "one"})
    cp = ws.snapshot(["a/**"], "a")
    (ws.root / "a/x.txt").write_text("two")
    (ws.root / "a/new.txt").write_text("new")
    (ws.root / "b/y.txt").write_text("two")  # another stage's file: must be untouched
    ws.restore(cp)
    assert (ws.root / "a/x.txt").read_text() == "one"
    assert not (ws.root / "a/new.txt").exists()
    assert (ws.root / "b/y.txt").read_text() == "two"


def test_diff_since_checkpoint(ws):
    ws.seed({"a.txt": "one\n"})
    cp = ws.snapshot(["**"], "all")
    (ws.root / "a.txt").write_text("two\n")
    d = ws.diff_since(cp)
    assert "-one" in d and "+two" in d


# ---- audit log ------------------------------------------------------------------------------
@pytest.fixture
def audit(tmp_path):
    db = Database(tmp_path / "a.db")
    log = AuditLog(db)
    for i in range(4):
        log.append("r1", "evt", "system", "n", {"i": i})
    yield log, db
    db.close()


def test_audit_chain_verifies(audit):
    log, _ = audit
    v = log.verify("r1")
    assert v.ok and v.events == 4


def test_audit_detects_modified_event(audit):
    log, db = audit
    db.execute("UPDATE events SET payload = ? WHERE run_id = 'r1' AND seq = 2", ('{"i": 99}',))
    v = log.verify("r1")
    assert not v.ok and v.broken_at == 2


def test_audit_detects_deleted_event(audit):
    log, db = audit
    db.execute("DELETE FROM events WHERE run_id = 'r1' AND seq = 3")
    v = log.verify("r1")
    assert not v.ok and v.broken_at == 4


def test_audit_chains_are_per_run(audit):
    log, _ = audit
    log.append("r2", "evt", "system")
    assert log.verify("r2").ok and log.verify("r1").ok


# ---- security scan --------------------------------------------------------------------------
def scan(src):
    return scan_python("x.py", ast.parse(src), Policy().forbidden_calls)


def test_scan_flags_dangerous_calls_and_sql_building():
    rules = {f["rule"] for f in scan(
        "import os, subprocess\neval('1')\nos.system('ls')\nsubprocess.run('x', shell=True)\n"
        "def f(c, n):\n    c.execute(f'SELECT * FROM t WHERE n = {n}')\n"
    )}
    assert rules == {"dangerous_call", "sql_injection"}


def test_scan_accepts_parameterised_sql():
    assert scan("def f(c, n):\n    c.execute('SELECT * FROM t WHERE n = ?', (n,))\n") == []


# ---- codebase analysis ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    d = tmp_path_factory.mktemp("base")
    for rel, text in baseline_seed().items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def test_scan_finds_routes_and_schema(baseline):
    scan_ = scan_codebase(baseline)
    routes = {(r["method"], r["path"]) for r in scan_["routes"]}
    assert ("POST", "/api/links") in routes and ("GET", "/{code}") in routes
    assert {"code", "url", "created_at", "click_count"} <= set(scan_["schema"]["links"])
    assert "expires_at" not in scan_["schema"]["links"]


def test_scan_knows_where_symbols_live(baseline):
    scan_ = scan_codebase(baseline)
    assert "service" in "".join(scan_["defined_in"]["create"])


# ---- metrics --------------------------------------------------------------------------------
def ev(seq, ts, type, node=None):
    return {"seq": seq, "ts": ts, "type": type, "node_id": node, "actor": "x", "payload": {}}


def test_run_metrics_from_synthetic_events():
    events = [
        ev(1, 0.0, "run_created"),
        ev(2, 1.0, "node_started", "a"),
        ev(3, 2.0, "node_failed", "a"),
        ev(4, 2.0, "rollback", "a"),
        ev(5, 2.0, "node_retry", "a"),
        ev(6, 3.0, "node_started", "a"),
        ev(7, 5.0, "node_succeeded", "a"),
        ev(8, 5.0, "approval_requested", "a"),
        ev(9, 9.0, "approval_decision", "a"),
        ev(10, 10.0, "run_succeeded"),
    ]
    m = metrics.run_metrics(events, {"status": "SUCCEEDED", "finished_at": 10.0})
    assert m["stage_executions"] == 2 and m["retries"] == 1 and m["rollbacks"] == 1
    assert m["mttr_seconds"] == 3.0  # failed at t=2, recovered at t=5
    assert m["end_to_end_seconds"] == 10.0 and m["human_wait_seconds"] == 4.0 and m["active_seconds"] == 6.0
    assert m["retry_frequency"] == 0.5


def test_unrecovered_failure_is_counted_not_averaged():
    m = metrics.run_metrics([ev(1, 0, "node_started", "a"), ev(2, 1, "node_failed", "a")], None)
    assert m["mttr_seconds"] is None and m["unrecovered_failures"] == 1


# ---- diagnosability -------------------------------------------------------------------------
def test_no_tests_ran_failure_says_why():
    from orchestrator import gates

    out = {"tests": {"total": 0, "failed": 0, "errors": 1, "passed": 0, "skipped": 0},
           "failures": [{"name": "pytest", "message": "ModuleNotFoundError: No module named 'httpx2'"}]}
    ctx = gates.GateContext({}, None, None, out, None, Policy())
    res = gates.evaluate("tests_pass", ctx)
    assert not res.passed and "httpx2" in res.detail
