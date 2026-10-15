# Engineering summary

## What was built

1. **An orchestration layer** (`orchestrator/`, about 4,400 lines of Python) that runs a software-engineering workflow as an explicit dependency graph with gates, bounded recovery, human checkpoints, policy enforcement, lineage, an audit trail and metrics.
2. **A set of deterministic agents** (`orchestrator/agents/`, `catalog/`) that fill the stages: requirements analysis, design and impact analysis, implementation, test authoring, documentation, security review, verification, migration check, release readiness.
3. **A URL shortener** (`shortener/`, about 450 lines) that serves as the brownfield baseline and the greenfield output.
4. **Surfaces**: REST API, dashboard, CLI, combined deployable server.
5. **137 tests**, CI workflow, Dockerfile, Render blueprint, and these docs.

## How the assignment's requirements are met

| Requirement | Where | Evidence |
|---|---|---|
| Explicit dependency graph | `pipeline.py`, `graph.py`; edges are data, serialised into each run | `test_orch_units.py` graph tests; dashboard graph view |
| Entry / exit gates | `gates.py` (19 gates); evaluated and logged for every stage | `gate_evaluated` events |
| Sequential and parallel paths with sync | Dataflow scheduling; join when all ancestors succeed | barrier test proves concurrency |
| Cross-stage context and decision lineage | Content-addressed artifacts with recorded inputs; decision records with supersession | `/lineage`, scenario 3 |
| Human approval checkpoints | Clarification, design (by impact), release (always); bound to artifact digest | scenario traces, API tests |
| Bounded retries, fallback, rollback, safe-stop | Failure ladder; scope-aware checkpoints; `HALTED` state | engine tests, drill |
| Policy guardrails (security, compliance, change control) | `policy.json`, workspace enforcement, security review, approval tags, change freeze | unit and engine tests, drill |
| Audit-grade observability | Hash-chained event log, `verify`, per-event actor | tamper tests |
| Reliability metrics | `metrics.py`: success rate, retry / rollback / rework frequency, MTTR, end-to-end latency | below |
| Dynamic re-planning | Graph change (migration check) and requirement change (amendment) | scenarios 2 and 3 |
| Three scenarios | Greenfield, brownfield, ambiguous (plus a drill) | `docs/SCENARIOS.md`, `tests/orch/test_orch_scenarios.py` |
| Runnable prototype, setup, testing, limitations, trade-offs | README, `docs/` | this repository |

## Results from a full run of all scenarios

Autopilot reviewer, default fault injection. The same numbers are at `/api/metrics` and `python -m orchestrator.cli metrics`.

| Scenario | Outcome | Tests in generated code | Stage executions | Retries | Reworks | Rollbacks | Re-plans | Policy violations | End-to-end |
|---|---|---|---|---|---|---|---|---|---|
| Greenfield | succeeded | 50/50 | 9 | 1 | 0 | 1 | 0 | 0 | 3.2 s |
| Brownfield | succeeded | 77/77 | 13 | 0 | 1 | 1 | 1 | 0 | 6.9 s |
| Ambiguous | succeeded | 61/61 | 17 | 0 | 0 | 0 | 0 | 0 | 6.2 s |
| Drill | halted (by design) | not reached | 7 | 0 | 2 | 3 | 0 | 3 | 0.2 s |

Across the four runs: success rate 75% (3 of 4; the drill is supposed to halt), retry frequency 2.2% and rollback frequency 10.9% of stage executions, MTTR 0.9 s (mean time from a stage's failure to its next success, over the two recovered failures).

**Read these carefully.** The faults were injected, so retry, rework and rollback rates measure the demos, not production reliability. Latency excludes real human think time because the scripted reviewer answers instantly; with a person in the loop the dashboard reports end-to-end and active latency separately. Verification (running pytest in a subprocess) dominates stage time.

## Decisions worth knowing

- **Gates decide, checkers produce evidence.** An agent cannot mark its own work as good.
- **Failures go to the stage that caused them.** The brownfield defect is noticed by the migration probe and fixed in implementation.
- **Approvals bind to digests.** A change after review needs a new review.
- **Early cutoff.** A re-run that yields an identical artifact does not disturb downstream stages or re-request approvals.
- **Deterministic agents.** Chosen for reproducibility and explainability; see limitations.

## Known gaps

Agents are not LLM-backed; subprocess isolation is not a sandbox; state is single-node SQLite; the audit chain is tamper-evident, not tamper-proof; static analysis is name-based. Full list and the next steps are in [TESTING_AND_TRADEOFFS.md](TESTING_AND_TRADEOFFS.md).

Each run also generates its own engineering summary (draft at the release stage, final after approval): `/api/runs/<id>/summary`, the "Engineering summary" tab, or `ENGINEERING_SUMMARY.md` inside the release bundle.
