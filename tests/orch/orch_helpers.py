"""Helpers shared by orchestrator tests (importable; fixtures live in conftest.py)."""
from __future__ import annotations

from orchestrator.agents import default_registry
from orchestrator.engine import TERMINAL
from orchestrator.scenarios import SCENARIOS, baseline_seed


class Wrap:
    """Wraps a real agent so a test can observe or disturb it without reimplementing it."""

    def __init__(self, inner, before=None, after=None):
        self.inner, self.name, self.before, self.after = inner, inner.name, before, after

    def run(self, ctx):
        if self.before:
            self.before(ctx)
        out = self.inner.run(ctx)
        if self.after:
            self.after(ctx, out)
        return out

    def __getattr__(self, item):  # e.g. `commit` on the release agent
        return getattr(self.inner, item)


def wrapped_registry(**wraps):
    reg = default_registry()
    for key, kw in wraps.items():
        reg.register(key.replace("__", "."), Wrap(reg.get(key.replace("__", ".")), **kw))
    return reg


def new_run(engine, scenario="greenfield", clean=False, **overrides):
    sc = SCENARIOS[scenario]
    return engine.create_run(
        scenario=sc.id, title=sc.title, requirement=overrides.get("requirement", sc.requirement), mode=sc.mode,
        seed_files=baseline_seed(engine.repo_root) if sc.seed == "baseline" else None,
        faults=overrides.get("faults", {} if clean else sc.faults),
    )


def drive(engine, run_id, approve=True, max_steps=40):
    """Advance a run, approving every checkpoint (and taking defaults) until it ends or `approve` is False."""
    for _ in range(max_steps):
        status = engine.advance(run_id)
        if status in TERMINAL:
            return status
        run = engine.get_run(run_id)
        if status == "WAITING_INPUT":
            engine.provide_clarification(run_id, {}, by="test", use_defaults=True)
        elif status == "WAITING_APPROVAL" and approve:
            for nid, n in run["nodes"].items():
                if n["status"] == "AWAITING_APPROVAL":
                    engine.decide_approval(run_id, nid, "approve", reviewer="test")
        else:
            return status
    raise AssertionError("run did not settle")


def events(engine, run_id, type=None):
    evs = engine.audit.events(run_id)
    return [e for e in evs if type is None or e["type"] == type]
