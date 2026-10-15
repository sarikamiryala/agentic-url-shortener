# Scenarios

Traces below are excerpts of real audit logs (`python -m orchestrator.cli show <run> --events`), trimmed to the events that matter. Sequence numbers are the log's own. Passing gate evaluations are omitted for space. Run them yourself with `python -m orchestrator.cli run <scenario> --autopilot`.

## 1. Greenfield: build the URL shortener

**Requirement.** Build a URL shortener: create short links, redirect, click analytics, validate input, rate-limit creation.

**Decomposition.** Requirements yields 5 functional requirements. Design yields 8 tasks, 5 risks, impact tags `new_system` and `public_api_change`, and six architecture decision records (SQLite, 307 not 301, random base62 codes, layering, in-memory rate limit, no IPs stored).

**Orchestration.** After the design approval, `implementation`, `test_authoring` and `docs` start together. An injected fault makes `docs` fail once after it has written a file: the engine rolls the file back and retries. `security_review` starts as soon as implementation lands (it does not wait for the tests); `verification` waits for both implementation and tests; `release_readiness` joins verification, security and docs.

```
 12 approval_requested   design            reasons: new_system, public_api_change
 13 approval_decision    design            human:autopilot approve
 16 start implementation
 19 start test_authoring
 24 start docs
 30 node_failed          docs              transient: simulated transient failure (attempt 1 of 1 injected)
 33 rollback             docs              deleted docs/API.md, reason: retry 1 of 2
 36 node_retry           docs              retry 1 of 2
 40 start docs (attempt 2)
 53 start security_review
 55 start verification
 67 start release_readiness
 70 approval_requested   release_readiness
 71 approval_decision    release_readiness approve
 72 action_committed     release_readiness release bundle, 16 files
 74 run_succeeded
```

**Validation.** 50 of 50 generated-code tests pass; the contract probe finds every endpoint; security review has 0 high findings; every exit gate is in the log.

**Point at:** the three concurrent starts (16, 19, 24); the rollback deleting the half-written file before the retry; `action_committed` only after the human decision.

## 2. Brownfield: add custom aliases and expiry

**Requirement.** Extend the existing shortener: custom alias for a link, links expire after N days, existing links and data must keep working.

**Decomposition.** The design agent reads the *actual* codebase (AST and schema parsing): routes, modules, the `links` table columns, and where `create`, `resolve`, `get_link`, `insert_link` are defined and used. It concludes that expiry needs a new column `expires_at` on `links`, tags the change `schema_change`, and lists the symbols each requirement touches.

**Re-plan.** Because of `schema_change`, the planner inserts a `migration_check` stage before the run goes any further:

```
  9 graph_replanned      design            added: migration_check
```

**The defect.** The primary implementation agent takes the shortest patch (an injected, seeded defect): it adds the column to the `CREATE TABLE` statement but does not upgrade existing databases. Every unit test passes on a fresh database. The migration probe opens a database created by the *baseline* code with the new code:

```
 52 gate_evaluated       migration_check   migration_ok FAILED: old_link_still_redirects: OperationalError: no such column: expires_at
 53 node_failed          migration_check
 54 rollback             implementation    restored shortener/api.py, ... reason: rework: 'migration_check' failed migration_ok
 55 rework               implementation    cause: migration_check, feedback passed to the agent
 56-59 invalidated       implementation, migration_check, security_review, verification
 61 start implementation (implementation.fallback) attempt 2
 77 output_unchanged     security_review   identical artifact, downstream not invalidated
 84 start verification (attempt 2)
```

The failure is attributed to the stage that *caused* it, not the stage that noticed it. The rework agent gets the failure as feedback and the fallback agent ships an idempotent in-place migration (`PRAGMA table_info` check, `ALTER TABLE ... ADD COLUMN`).

**Validation.** 77 of 77 tests pass (the 50 original plus 27 new). The original tests still pass, which is the backward-compatibility evidence. Migration probe passes. Release approved.

**Point at:** the graph change at event 9; the gate failing on evidence a fresh-database test cannot produce; rework target being `implementation` while the failing stage was `migration_check`; the decision record `strategy: direct` superseded by `migration_aware`.

## 3. Ambiguous: "Make links safer."

**Requirement.** Three words.

**Decomposition.** There is nothing to decompose yet. Requirements finds the capability (link safety) but three behaviour-changing choices it will not guess: which threat, where to enforce, how to match domains. The `no_blocking_questions` gate fails and the run waits:

```
  4 gate_evaluated       requirements      no_blocking_questions FAILED: 3 question(s) need a human answer: threat, enforce, match
  6 waiting_input        requirements
  7 input_received       requirements      threat=malicious_destinations, enforce=create_only, match=host_and_subdomains
```

Two of the threat options (service abuse, analytics privacy) are marked unsupported by the available agents. Choosing one does not unblock the run; it stays blocked with a note (`tests/orch/test_orch_engine.py::test_unsupported_clarification_option_does_not_unblock`).

**The change of mind.** The design is approved with creation-time checking, the build goes green, and then the reviewer at the release gate requests changes: links created *before* a domain was blocklisted must also stop working. That amendment re-plans the whole chain:

```
 64 approval_decision    release_readiness request_changes + amendment
 65 requirement_amended  requirements
 66 invalidated          requirements
 67 invalidated          release_readiness
 68 start requirements (attempt 3)
 72 invalidated          design            inputs changed: requirements
 79-81 invalidated       implementation, test_authoring, docs   inputs changed: design
 82-83 approval_requested/decision  design (a new artifact needs a new approval)
 86-92 restart           implementation, test_authoring, docs (attempt 2)
105-106 invalidated      security_review, verification
119 output_unchanged     security_review   identical artifact, release not invalidated by it
126 start release_readiness (attempt 2)
130 approval_decision    release_readiness approve
```

The decision log keeps both answers: `answer:enforce = create_only` is superseded by `answer:enforce = create_and_redirect`, with the amendment text as the reason. 61 of 61 tests pass, now including redirect-time blocking.

**Point at:** the system refusing to guess; the new design artifact needing a fresh approval after the re-plan; both decisions surviving in the lineage.

## Drill: guardrails, rollback and safe-stop (extra)

Not one of the three required scenarios; it exists to show the stop condition. Every implementation agent (primary and fallback) tries to write `shortener/_debug.py` with an AWS-style key.

```
 40 policy_violation     implementation    shortener/_debug.py: aws_access_key pattern matched
 45 rollback             implementation    deleted shortener/__init__.py, api.py, ... (partial work)
 46 rework               implementation    next agent: implementation.fallback
 58 policy_violation     implementation
 73 policy_violation     implementation
 76 run_halted           policy_violation_budget_exceeded (3 violations)
```

The credential never reached disk, partial work was rolled back each time, the unaffected stages (`docs`, `test_authoring`) are kept, `security_review`, `verification` and `release_readiness` never ran, and nothing was released. The audit chain of the halted run still verifies.

## What the injected faults are

`greenfield`: transient failure in `docs`. `brownfield`: seeded defect in the primary implementation agent. `drill`: credential leak. `ambiguous`: none; the interesting events come from human input. Run with `--clean` (CLI) or the "clean run" checkbox (dashboard) to see the same scenarios without faults; the retry and rework events disappear and everything else is unchanged.
