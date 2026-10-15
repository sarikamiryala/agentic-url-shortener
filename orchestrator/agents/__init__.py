"""Default agent registry."""
from __future__ import annotations

from .base import AgentRegistry
from .builders import AuthorTestsAgent, DocsAgent, ImplementationAgent
from .checkers import MigrationCheckAgent, ReleaseReadinessAgent, SecurityReviewAgent, VerificationAgent
from .design import DesignAgent
from .requirements import RequirementsAgent


def default_registry() -> AgentRegistry:
    r = AgentRegistry()
    r.register("requirements", RequirementsAgent())
    r.register("design", DesignAgent())
    r.register("implementation", ImplementationAgent("primary"))
    r.register("implementation.fallback", ImplementationAgent("fallback"))
    r.register("test_authoring", AuthorTestsAgent())
    r.register("docs", DocsAgent())
    r.register("security_review", SecurityReviewAgent())
    r.register("verification", VerificationAgent())
    r.register("migration_check", MigrationCheckAgent())
    r.register("release_readiness", ReleaseReadinessAgent())
    return r
