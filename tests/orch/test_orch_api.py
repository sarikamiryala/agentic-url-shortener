"""HTTP API, dashboard and the combined server."""
import time

import pytest
from fastapi.testclient import TestClient

import server
from orchestrator.api import create_app


def wait_for(client, rid, statuses, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        run = client.get(f"/api/runs/{rid}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.2)
    raise AssertionError(f"run stuck in {run['status']}")


@pytest.fixture
def client(make_engine):
    with TestClient(create_app(make_engine())) as c:
        yield c


def test_dashboard_and_scenario_list(client):
    assert "Orchestrator Dashboard" in client.get("/").text
    ids = [s["id"] for s in client.get("/api/scenarios").json()]
    assert ids == ["greenfield", "brownfield", "ambiguous", "drill"]


def test_scripted_run_exposes_every_artifact_the_dashboard_needs(client):
    rid = client.post("/api/runs", json={"scenario": "greenfield"}).json()["run_id"]
    run = wait_for(client, rid, {"SUCCEEDED", "HALTED"})
    assert run["status"] == "SUCCEEDED"
    assert run["graph"]["layers"][0] == ["requirements"]
    assert client.get(f"/api/runs/{rid}/audit/verify").json()["ok"] is True
    assert client.get(f"/api/runs/{rid}/events").json()[0]["type"] == "run_created"
    m = client.get(f"/api/runs/{rid}/metrics").json()
    assert m["retries"] == 1 and m["end_to_end_seconds"] > 0 and m["mttr_seconds"] is not None
    g = client.get("/api/metrics").json()
    assert g["success_rate"] == 1.0 and g["runs"] == 1
    assert "shortener/api.py" in client.get(f"/api/runs/{rid}/files").json()
    assert "def create_app" in client.get(f"/api/runs/{rid}/file", params={"path": "shortener/api.py"}).text
    assert "+++ b/shortener/api.py" in client.get(f"/api/runs/{rid}/diff").text
    assert "Engineering summary" in client.get(f"/api/runs/{rid}/summary").text
    assert client.get(f"/api/runs/{rid}/bundle").headers["content-type"] == "application/zip"
    lineage = client.get(f"/api/runs/{rid}/lineage").json()
    assert {a["id"] for a in lineage["artifacts"]} >= {"design@1", "verification@1"}
    art = client.get(f"/api/runs/{rid}/artifacts/design@1").json()
    assert art["current"] and art["data"]["pack"] == "core_service"


def test_a_person_can_drive_a_run_through_the_api(client):
    rid = client.post("/api/runs", json={"scenario": "greenfield", "autopilot": False, "clean": True}).json()["run_id"]
    run = wait_for(client, rid, {"WAITING_APPROVAL"})
    assert run["pending_approvals"] == ["design"]
    assert client.get(f"/api/runs/{rid}/bundle").status_code == 404  # nothing released yet
    assert client.post(f"/api/runs/{rid}/approvals/design", json={"decision": "approve", "by": "sarika"}).status_code == 200
    run = wait_for(client, rid, {"WAITING_APPROVAL", "HALTED"})
    assert run["pending_approvals"] == ["release_readiness"]
    client.post(f"/api/runs/{rid}/approvals/release_readiness", json={"decision": "approve", "by": "sarika"})
    assert wait_for(client, rid, {"SUCCEEDED", "HALTED"})["status"] == "SUCCEEDED"
    assert client.get(f"/api/runs/{rid}/bundle").status_code == 200
    actors = {e["actor"] for e in client.get(f"/api/runs/{rid}/events").json() if e["type"] == "approval_decision"}
    assert actors == {"human:sarika"}


def test_clarification_flow_through_the_api(client):
    rid = client.post("/api/runs", json={"scenario": "ambiguous", "autopilot": False}).json()["run_id"]
    run = wait_for(client, rid, {"WAITING_INPUT"})
    assert [q["id"] for q in run["open_questions"]] == ["threat", "enforce", "match"]
    r = client.post(f"/api/runs/{rid}/clarify", json={"answers": {"nonsense": "x"}})
    assert r.status_code == 409
    assert client.post(f"/api/runs/{rid}/clarify", json={"answers": {}, "use_defaults": True}).status_code == 200
    assert wait_for(client, rid, {"WAITING_APPROVAL"})["pending_approvals"] == ["design"]


def test_stop_button(client):
    rid = client.post("/api/runs", json={"scenario": "greenfield", "autopilot": False}).json()["run_id"]
    wait_for(client, rid, {"WAITING_APPROVAL"})
    assert client.post(f"/api/runs/{rid}/stop", json={"reason": "enough", "by": "sarika"}).status_code == 200
    run = wait_for(client, rid, {"HALTED"})
    assert "enough" in run["halt_reason"]
    assert client.post(f"/api/runs/{rid}/stop", json={}).status_code == 409


def test_errors_are_clean(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.post("/api/runs", json={"scenario": "nope"}).status_code == 404
    assert client.post("/api/runs", json={"scenario": "greenfield", "requirement": "x" * 5000}).status_code == 422
    rid = client.post("/api/runs", json={"scenario": "greenfield", "autopilot": False}).json()["run_id"]
    wait_for(client, rid, {"WAITING_APPROVAL"})
    assert client.post(f"/api/runs/{rid}/approvals/design", json={"decision": "maybe"}).status_code == 409
    assert client.get(f"/api/runs/{rid}/artifacts/ghost@9").status_code == 404


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "shortener/../../x", "policy.json"])
def test_file_endpoint_cannot_read_outside_the_workspace(client, path):
    rid = client.post("/api/runs", json={"scenario": "greenfield", "autopilot": False}).json()["run_id"]
    wait_for(client, rid, {"WAITING_APPROVAL"})
    assert client.get(f"/api/runs/{rid}/file", params={"path": path}).status_code == 404


def test_admin_token_protects_actions_but_not_reads(make_engine, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    with TestClient(create_app(make_engine())) as c:
        assert c.get("/api/config").json()["admin_token_required"] is True
        assert c.get("/api/runs").status_code == 200
        assert c.post("/api/runs", json={"scenario": "greenfield", "autopilot": False}).status_code == 401
        assert c.post("/api/runs", json={"scenario": "greenfield", "autopilot": False},
                      headers={"X-Admin-Token": "wrong"}).status_code == 401
        ok = c.post("/api/runs", json={"scenario": "greenfield", "autopilot": False}, headers={"X-Admin-Token": "s3cret"})
        assert ok.status_code == 202


def test_run_creation_is_rate_limited(make_engine, monkeypatch):
    monkeypatch.setenv("ORCH_RUNS_PER_MINUTE", "1")
    with TestClient(create_app(make_engine())) as c:
        assert c.post("/api/runs", json={"scenario": "greenfield", "autopilot": False}).status_code == 202
        r = c.post("/api/runs", json={"scenario": "greenfield", "autopilot": False})
        assert r.status_code == 429 and "Retry-After" in r.headers


def test_concurrent_run_cap(make_engine, monkeypatch):
    monkeypatch.setenv("ORCH_MAX_ACTIVE_RUNS", "0")
    with TestClient(create_app(make_engine())) as c:
        assert c.post("/api/runs", json={"scenario": "greenfield"}).status_code == 429


# ---- combined server ------------------------------------------------------------------------
@pytest.fixture
def combined(tmp_path):
    with TestClient(server.create_app(str(tmp_path / "d")), follow_redirects=False) as c:
        yield c


def test_combined_server_routes(combined):
    assert "Agentic software engineering system" in combined.get("/").text
    assert combined.get("/healthz").json() == {"status": "ok"}
    assert combined.get("/orchestrator").status_code == 307
    assert "Orchestrator Dashboard" in combined.get("/orchestrator/").text
    assert combined.get("/orchestrator/api/scenarios").status_code == 200


def test_shortener_still_works_beside_the_orchestrator(combined):
    link = combined.post("/api/links", json={"url": "https://example.org/page"}).json()
    r = combined.get(f"/{link['code']}")
    assert r.status_code == 307 and r.headers["location"] == "https://example.org/page"
    assert combined.get("/unknown1").status_code == 404
