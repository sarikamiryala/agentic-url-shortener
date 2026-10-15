# Testing approach, limitations and trade-offs

## Testing approach

139 tests, `python -m pytest`, about a minute.

| Layer | Tests | What it covers |
|---|---|---|
| Product (`tests/product`) | 50 | Validation (including SSRF-style hosts), storage, service, rate limiter, HTTP API. Each test file declares `# covers:` tags used for requirement traceability |
| Orchestrator units (`tests/orch/test_orch_units.py`) | 32 | Graph (layers, cycles, re-plan), policy and secret scanner, scoped writes and scope-aware rollback, audit tamper detection, security scan, codebase analysis, metric definitions on synthetic events |
| Engine behaviour (`test_orch_engine.py`) | 25 | Real agents wrapped to observe or disturb them: parallel overlap (barrier), join ordering, bounded retries, relative data directory, fallback, rework with feedback, futile-rework detection, policy budget, refusal, change freeze, deadline, stop with in-flight work, approval binding, invalid human actions, lineage, early cutoff, restart recovery |
| Scenarios (`test_orch_scenarios.py`) | 17 | All four scenarios run once and inspected: statuses, attribution, bundle contents, re-plan, defect caught by the right stage, amendment re-planning, decision supersession |
| API (`test_orch_api.py`) | 15 | Full run through HTTP, human-driven run, clarification, stop, error handling, path traversal, admin token, rate limit, run cap, combined server routing |

The pipeline also tests *its output*: the `verification` stage runs the generated tests in a subprocess and probes the generated app against the API contract, and `migration_check` runs the new code against a database made by the old code. Those gates are why the brownfield defect is caught.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11-3.13, then runs all scenarios through the CLI and verifies the audit chains.

**Tests that check the tests.** Several tests exist to prove a claim rather than a function: the barrier test cannot pass unless stages overlap; the recovery test persists a "crashed" mid-flight state and reopens the database in a fresh engine; the amendment test asserts the *sequence* of approvals and attempts, not just the final status.

**Not tested:** behaviour under real concurrent load, a real LLM agent, multiple server workers, browser rendering of the dashboard beyond a manual headless walk-through of the human-in-the-loop flow.

## Limitations (known, deliberate)

1. **Agents are deterministic stand-ins, not LLMs.** Reproducibility was the priority for a take-home that must be explained and re-run. The cost: the system cannot handle a requirement outside its feature catalog (it asks a human, or refuses and escalates). What the prototype demonstrates is the control plane, which is the part that makes LLM agents safe to run.
2. **Faults are injected.** The retry, rework and safe-stop paths are exercised by fault injection, so the reliability numbers in the dashboard describe the demos, not production. The dashboard says so.
3. **Subprocess isolation is not a security sandbox.** Generated tests run in a child process with a scrubbed environment and a timeout, but with the same user and filesystem access. Untrusted agents need a container or VM per run.
4. **Single node.** State and audit are one SQLite file; rate limiting is in process; run both with one worker. Scaling out needs a shared database, a queue and a shared limiter.
5. **Tamper evidence, not prevention.** The hash chain detects edits and deletions; it cannot stop someone who rewrites the whole database. Fix: periodically anchor the chain head externally.
6. **Static analysis is name-based.** The codebase scan uses AST and names, so the call graph over-approximates (same-named functions in different modules look alike) and cannot see dynamic dispatch.
7. **Only the latest output of each stage is retained.** The latest output of each stage is stored; earlier versions keep their digest and inputs in the lineage but not their content.
8. **Run cap counts running runs, not waiting ones.** A run waiting for approval consumes disk but no CPU, so it is not counted. Run creation is rate limited per client address instead.
9. **Dashboard approvals are not authenticated per person** unless `ADMIN_TOKEN` is set, and then it is one shared token. Reviewer names are self-declared.
10. **Free-tier hosting resets state** on redeploy and restart (no persistent disk).

## Trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Agents | Deterministic | LLM-backed | Reproducible scenarios, no API cost or keys, explainable line by line. Interface is the seam |
| Stack | Python, FastAPI, SQLite | Java/Spring, Postgres | One-day build, zero ops. SQLite's single writer is fine at this scale |
| Concurrency | Threads + `FIRST_COMPLETED` waits | asyncio, Celery/Temporal | Agent work is subprocess and file I/O; threads are simple and debuggable. A durable workflow engine is the production answer |
| State | JSON run document + event table | Event sourcing | State lives in the run document; the event log is evidence and metrics source, not the state store. Simpler, but the two are written separately and could in principle disagree |
| Rework target | Responsible upstream stage | Retry the failing stage | The failing stage is usually a witness, not the cause |
| Approval binding | To artifact digest | To stage name | Prevents approving one design and shipping another |
| Early cutoff | Compare output digests | Always re-run downstream | Saves work and keeps approvals from being re-requested without cause |
| Redirect status | 307 | 301/308 | Browsers cache 301/308 and would hide repeat clicks from analytics |
| Analytics | No IP stored | Store IP | Smaller compliance surface; enforced by a policy gate |
| Metrics | Derived from audit | Separate counters | One source of truth, every figure traceable |

## What I would do next

LLM-backed agents behind the same interface with the same gates; per-run container isolation; Postgres plus a queue; external anchoring of the audit head; per-user authentication on approvals; persisting every artifact version's content.
