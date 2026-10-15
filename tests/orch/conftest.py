"""Shared fixtures for orchestrator tests."""
from __future__ import annotations

import pytest
from orch_helpers import new_run

from orchestrator.engine import Engine
from orchestrator.scenarios import SCENARIOS


@pytest.fixture
def make_engine(tmp_path):
    engines: list[Engine] = []

    def make(registry=None, policy=None, name="data"):
        e = Engine(tmp_path / name, policy=policy, registry=registry)
        e.raise_internal_errors = True
        engines.append(e)
        return e

    yield make
    for e in engines:
        e.close()


@pytest.fixture(scope="module")
def scenario_runs(tmp_path_factory):
    """Each scenario run once with the scripted reviewer; shared by the end-to-end tests."""
    from orchestrator.scenarios import AutoPilot

    engine = Engine(tmp_path_factory.mktemp("scenarios"))
    ids = {}
    for sid, sc in SCENARIOS.items():
        rid = new_run(engine, sid)
        AutoPilot(engine, sc.script).run(rid)
        ids[sid] = rid
    yield engine, ids
    engine.close()
