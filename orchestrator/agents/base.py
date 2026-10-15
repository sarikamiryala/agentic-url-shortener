"""Agent contract.

An agent is a unit of delegated work with a narrow job and a narrow write scope.
It receives an `AgentContext` (requirements, upstream outputs, feedback from earlier
failures, a scoped workspace) and returns an `AgentOutput`. It never decides whether
its own output is acceptable; gates and humans do that.

The agents shipped here are deterministic. To use an LLM, implement `run` with the same
signature and register it under the same key; nothing in the orchestrator changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..policy import Policy
from ..workspace import ScopedWorkspace, Workspace


class TransientAgentError(Exception):
    """A failure worth retrying (timeout, flaky dependency)."""


class AgentRefusal(Exception):
    """The agent cannot do this job. Escalate to a human; do not retry."""


@dataclass
class AgentOutput:
    data: dict  # the artifact: deterministic, hashed, handed to downstream stages
    summary: str = ""
    decisions: list[dict] = field(default_factory=list)  # {key, title, choice, rationale, alternatives}
    evidence: dict = field(default_factory=dict)  # non-hashed detail (durations, logs)


@dataclass
class AgentContext:
    run_id: str
    node_id: str
    agent_key: str
    attempt: int
    is_fallback: bool
    rework: int
    requirement: str
    amendments: list[str]
    answers: dict
    answer_sources: dict
    mode: str
    inputs: dict[str, dict]
    feedback: dict | None
    run_facts: dict
    workspace: ScopedWorkspace
    full_workspace: Workspace
    baseline_dir: Path  # the codebase exactly as it was when the run started (immutable)
    checkpoints: dict[str, str]
    faults: dict
    policy: Policy
    repo_root: Path
    run_dir: Path
    config: dict

    @property
    def report_dir(self) -> Path:
        d = self.run_dir / "reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def fault(self, name: str) -> dict | None:
        """Fault-injection settings for this stage, e.g. {'transient': 1}."""
        stage_faults = self.faults.get(self.node_id) or {}
        value = stage_faults.get(name)
        return value if value else None

    def fail_transiently_if_configured(self) -> None:
        times = self.fault("transient")
        if times and self.attempt <= int(times):
            raise TransientAgentError(
                f"simulated transient failure in {self.node_id} (attempt {self.attempt} of {times} injected)"
            )

    def checkpoint_dir(self, node_id: str) -> Path | None:
        cp = self.checkpoints.get(node_id)
        return self.full_workspace.checkpoint_files_dir(cp) if cp else None


class Agent(Protocol):
    name: str

    def run(self, ctx: AgentContext) -> AgentOutput: ...


def decision(key: str, title: str, choice: str, rationale: str, alternatives: list[str] | None = None) -> dict:
    return {
        "key": key,
        "title": title,
        "choice": choice,
        "rationale": rationale,
        "alternatives": alternatives or [],
    }


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, key: str, agent: Agent) -> None:
        self._agents[key] = agent

    def get(self, key: str) -> Agent:
        return self._agents[key]

    def keys(self) -> list[str]:
        return sorted(self._agents)
