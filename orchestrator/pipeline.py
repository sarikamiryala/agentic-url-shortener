"""The SDLC graph and the re-planning rules that adjust it while a run is in flight."""
from __future__ import annotations

from .graph import Graph, NodeSpec
from .policy import Policy


def build_graph(policy: Policy) -> Graph:
    r, m = policy.default_max_retries, policy.default_max_rework
    return Graph(
        [
            NodeSpec(
                "requirements", "Requirements understanding", "requirements",
                exit_gates=["no_blocking_questions", "spec_complete"], max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "design", "Design, impact analysis and task decomposition", "design", deps=["requirements"],
                exit_gates=["design_complete", "tasks_acyclic", "risks_have_validation"],
                approval="by_impact", max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "implementation", "Implementation", "implementation", deps=["design"],
                write_scope=["shortener/**"], entry_gates=["change_freeze_off"],
                exit_gates=["files_written", "compiles", "policy_clean", "tasks_covered"],
                fallback_agent="implementation.fallback", rework_target="implementation",
                max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "test_authoring", "Test authoring (from the contract, blind to the implementation)", "test_authoring",
                deps=["design"], write_scope=["tests/**"], entry_gates=["change_freeze_off"],
                exit_gates=["tests_traceable"], max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "docs", "Documentation", "docs", deps=["design"],
                write_scope=["docs/**", "README.md"], entry_gates=["change_freeze_off"],
                exit_gates=["docs_cover_contract"], max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "security_review", "Security and compliance review", "security_review", deps=["implementation"],
                entry_gates=["workspace_has_code"], exit_gates=["no_high_findings"],
                rework_target="implementation", max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "verification", "Verification: tests and API contract", "verification",
                deps=["implementation", "test_authoring"], entry_gates=["workspace_has_code"],
                exit_gates=["tests_pass", "contract_conformance"], rework_target="implementation",
                max_retries=r, max_rework=m,
            ),
            NodeSpec(
                "release_readiness", "Release readiness", "release_readiness",
                deps=["verification", "security_review", "docs"], entry_gates=["upstream_green"],
                exit_gates=["release_checklist"], approval="always", max_retries=r, max_rework=m,
            ),
        ]
    )


def migration_node(policy: Policy) -> NodeSpec:
    return NodeSpec(
        "migration_check", "Migration check against a pre-change database", "migration_check",
        deps=["implementation"], entry_gates=["workspace_has_code"], exit_gates=["migration_ok"],
        rework_target="implementation", max_retries=policy.default_max_retries,
        max_rework=policy.default_max_rework, dynamic=True,
    )


def apply_adjustments(graph: Graph, design: dict, policy: Policy) -> dict:
    """Re-plan: mutate `graph` according to the design's impact. Returns what changed."""
    wants_migration = "schema_change" in design["impact_tags"] and design["mode"] == "brownfield"
    added: list[str] = []
    removed: list[str] = []
    if wants_migration and "migration_check" not in graph.nodes:
        graph.add(migration_node(policy), validate=False)
        graph.nodes["release_readiness"].deps.append("migration_check")
        added.append("migration_check")
    elif not wants_migration and "migration_check" in graph.nodes:
        graph.remove("migration_check")
        removed.append("migration_check")
    graph.validate()
    return {"added": added, "removed": removed}
