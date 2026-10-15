# Agentic Software Engineering System

An orchestration layer that takes a requirement, plans it as a dependency graph of engineering stages, and drives the stages through gates, bounded recovery, human approvals and an audit-grade event log. The system it builds and changes is a small URL shortener.

> **What is real and what is a stand-in.** The orchestration layer is real and does the hard parts: scheduling, gates, retries, rollback, re-planning, approvals, policy enforcement, lineage, audit, metrics. The *agents* it schedules are deterministic programs, not LLM calls. They produce reviewed reference code from a feature catalog. The agent interface is the seam for an LLM (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#swapping-in-an-llm)). Failures in the scenarios are injected on purpose and labelled as such.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest                       # 138 tests
uvicorn server:app --port 8000         # then open http://localhost:8000
```

- `/` landing page and the working shortener
- `/orchestrator/` dashboard: start a scenario, watch the graph run, take the human decisions yourself
- `/docs` OpenAPI for the shortener, `/orchestrator/docs` for the orchestrator

Or from the terminal:

```bash
python -m orchestrator.cli scenarios
python -m orchestrator.cli run greenfield  --autopilot
python -m orchestrator.cli run brownfield  --autopilot
python -m orchestrator.cli run ambiguous   --autopilot
python -m orchestrator.cli run drill       --autopilot   # ends HALTED on purpose
python -m orchestrator.cli metrics
python -m orchestrator.cli verify-audit
```

`--autopilot` lets a scripted reviewer make the human decisions (recorded as `human:autopilot`). Without it the run stops at each checkpoint and you decide with `approve`, or in the dashboard. `--clean` turns off injected faults.

## The three scenarios

| Scenario | Requirement | What it shows |
|---|---|---|
| Greenfield | "Build a URL shortener…" | Decomposition into 8 stages, parallel branches and a join, a transient fault retried with rollback, two approvals |
| Brownfield | "Add custom aliases and expiry, keep existing links working" | Impact analysis from the real codebase, a **re-plan** that inserts a migration check, a defect that unit tests miss but the migration probe catches, rework with feedback and a fallback agent |
| Ambiguous | "Make links safer." | Refuses to guess, asks blocking questions, rejects an option it cannot build, then a reviewer **changes the requirement at the last gate** and the chain re-plans with decision lineage kept |
| Drill (extra) | Greenfield with a credential-leaking agent | Policy engine blocks every write, partial work rolls back, run safe-stops when the budget is spent |

Real traces of each are in [docs/SCENARIOS.md](docs/SCENARIOS.md).

## Layout

```
orchestrator/   engine, graph, gates, policy, audit, workspace, metrics, API, dashboard, CLI
orchestrator/agents/   the agent contract and the deterministic agents
catalog/        reviewed implementation packs the agents draw from (tested in isolation by the pipeline)
shortener/      the v1 service: the baseline for brownfield runs and the greenfield output
tests/product/  tests for the shortener      tests/orch/  tests for the orchestrator
server.py       combined app (shortener + orchestrator)      policy.json  guardrails as data
docs/           architecture, scenarios, testing and trade-offs, deployment
```

## Docs

[Architecture](docs/ARCHITECTURE.md) · [Scenarios](docs/SCENARIOS.md) · [Testing, limitations and trade-offs](docs/TESTING_AND_TRADEOFFS.md) · [Engineering summary](docs/ENGINEERING_SUMMARY.md) · [Deployment](docs/DEPLOYMENT.md)
